"""Verify the deployable Flux source and retained state in the rendered production root."""

import json
import runpy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "bootstrap/nebius/management/render-flux-source.py"


class ProductionFluxTests(unittest.TestCase):
    def test_reconciliation_uses_published_shape_and_preserves_state(self):
        head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
        for fetch in (True, False):
            with self.subTest(fetch=fetch):
                cmd = [sys.executable, RENDER, head]
                if fetch:
                    cmd.append("--fetch")
                source, reconcile = json.loads(subprocess.check_output(cmd))["items"]
                self.assertEqual(source["spec"]["ref"], {"commit": head})
                self.assertEqual(source["spec"]["secretRef"]["name"], "nebius-git-read")
                self.assertEqual(reconcile["spec"]["sourceRef"]["name"], source["metadata"]["name"])
                self.assertEqual(reconcile["spec"]["decryption"]["secretRef"]["name"], "nebius-sops-age")
                self.assertEqual(reconcile["spec"]["path"], "./clusters/prod-nebius")
                self.assertEqual(reconcile["spec"]["deletionPolicy"], "Orphan")
                self.assertFalse(reconcile["spec"]["prune"])
                self.assertEqual(reconcile["spec"]["serviceAccountName"], "nebius-platform")
                self.assertFalse(reconcile["spec"]["wait"])
                self.assertEqual(source["metadata"]["name"], "platform-" + head)
                self.assertIs(source["spec"]["suspend"], not fetch)
                self.assertIs(reconcile["spec"]["suspend"], True)
        for bad in ("main", "0" * 40):
            self.assertNotEqual(subprocess.run([sys.executable, RENDER, bad], capture_output=True).returncode, 0)

    def test_claims_bind_on_first_consumer_and_survive_pruning(self):
        rendered = subprocess.check_output(["kubectl", "kustomize", ROOT / "platform/overlays/prod"])
        objects = list(yaml.safe_load_all(rendered))
        classes = {o["metadata"]["name"]: o for o in objects if o["kind"] == "StorageClass"}
        claims = [o for o in objects if o["kind"] == "PersistentVolumeClaim"]
        self.assertEqual({o["metadata"]["name"] for o in claims}, {"airaware-checkpoints", "airaware-model-state"})
        for claim in claims:
            storage = classes[claim["spec"]["storageClassName"]]
            self.assertEqual(storage["reclaimPolicy"], "Retain")
            self.assertEqual(storage["volumeBindingMode"], "WaitForFirstConsumer")
            self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "64Gi")
        for obj in objects:
            if obj["kind"] in ("Namespace", "PersistentVolumeClaim", "StorageClass"):
                self.assertEqual(obj["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"], "disabled")
        self.assertFalse({"Job", "Pod", "Deployment", "StatefulSet"} & {o["kind"] for o in objects})

    def test_pilot_reconciles_only_three_existing_objects(self):
        flux = ROOT / "bootstrap/nebius/management/flux"
        rendered = yaml.safe_load_all(subprocess.check_output(["kubectl", "kustomize", ROOT / "clusters/prod-nebius"]))
        targets = {(o["kind"], o["metadata"]["namespace"], o["metadata"]["name"]) for o in rendered}
        self.assertEqual(targets, {("LimitRange", "sky-cpu", "cpu"), ("ResourceQuota", "sky-cpu", "pilot"),
                                   ("NetworkPolicy", "sky-cpu", "dns-only")})
        objects = json.loads(subprocess.check_output([sys.executable, flux / "render-controllers.py"]))["items"]
        self.assertFalse(any(o["kind"].startswith("ClusterRole") for o in objects))
        role = next(o for o in objects if o["kind"] == "Role" and o["metadata"]["namespace"] == "sky-cpu")
        self.assertEqual({(r["resources"][0], r["resourceNames"][0]) for r in role["rules"]},
                         {("limitranges", "cpu"), ("resourcequotas", "pilot"), ("networkpolicies", "dns-only")})
        self.assertTrue(all(set(r["verbs"]) == {"get", "patch"} for r in role["rules"]))
        deployments = [o for o in objects if o["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 2)
        for deploy in deployments:
            pod = deploy["spec"]["template"]["spec"]
            container, = pod["containers"]
            self.assertIn("@sha256:", container["image"])
            self.assertIn("--watch-all-namespaces=false", container["args"])
            self.assertEqual(pod["nodeSelector"]["airaware.training/pool"], "training-cpu")
        platform = list(yaml.safe_load_all(subprocess.check_output(["kubectl", "kustomize", ROOT / "platform/overlays/prod"])))
        self.assertFalse(any(o["metadata"]["name"] == "episode-drain" for o in platform))
        self.assertFalse(any(s.get("name") == "episode-submit" for o in platform for s in o.get("subjects", ())))
        fence = next(o for o in platform if o["kind"] == "ValidatingAdmissionPolicy" and o["metadata"]["name"] == "episode-submit-inputs")
        self.assertEqual(fence["spec"]["validations"][0]["expression"], "false")

    def test_pilot_resource_limits(self):
        rendered = subprocess.check_output(["kubectl", "kustomize", ROOT / "clusters/prod-nebius"])
        specs = {o["kind"]: o["spec"] for o in yaml.safe_load_all(rendered)}
        self.assertEqual(specs["ResourceQuota"]["hard"], {
            "pods": "3",
            "count/jobs.batch": "2",
            "requests.cpu": "2",
            "limits.cpu": "3",
            "requests.memory": "5Gi",
            "limits.memory": "8Gi",
            "requests.nvidia.com/gpu": "0",
            "persistentvolumeclaims": "0",
            "services": "3",
            "services.loadbalancers": "0",
            "services.nodeports": "0",
        })
        self.assertEqual(specs["LimitRange"]["limits"], [{
            "type": "Container",
            "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
            "default": {"cpu": "1", "memory": "2Gi"},
            "max": {"cpu": "2", "memory": "4Gi"},
        }])

    def test_native_training_probe_covers_the_managed_render_under_the_hold(self):
        probe = runpy.run_path(str(ROOT / "workloads/smoke/verify-training-access.py"))
        policy = next(o for o in yaml.safe_load_all((ROOT / "platform/overlays/prod/cohort-access.yaml").read_text())
                      if o["kind"] == "ValidatingAdmissionPolicy")
        self.assertEqual(policy["spec"]["validations"][0]["expression"], "false")
        images = {v["name"]: v["expression"].strip("'") for v in policy["spec"]["variables"]
                  if v["name"] in {"trainerImage", "preflightImage"}}
        self.assertNotEqual(images["trainerImage"], images["preflightImage"])
        cases = {}

        def collect(job, name, denial=None):
            self.assertNotIn(name, cases)
            cases[name] = (job["spec"]["template"]["spec"]["containers"][0], denial)

        probe["exercise_release"](images["trainerImage"], images["preflightImage"], collect)
        for role, mode in (("trainerImage", "job"), ("preflightImage", "preflight")):
            for name in (mode, "paired-" + mode, "retry-" + mode, "fresh-retry-" + mode, "inference-" + mode):
                container, denial = cases[name]
                self.assertIsNone(denial)
                self.assertEqual(container["image"], images[role])
                self.assertEqual([e["value"] for e in container["env"] if e["name"] == "EPISODE_IMAGE"],
                                 [images["trainerImage"]])
            self.assertNotIn("--resume", cases["fresh-retry-" + mode][0]["command"])
            self.assertIn("--resume-checkpoint", cases["retry-" + mode][0]["command"])
            self.assertEqual(cases["inference-" + mode][0]["command"][2:4], ["infer", "quantile"])
            for label in ("parallelism", "completions", "backoffLimit", "suspend", "cpu-request",
                          "memory-request", "ephemeral-storage-request"):
                self.assertIsNotNone(cases[label + "-" + mode][1])
        for label in ("gpu-deadline-None", "gpu-deadline-356401", "preflight-memory-limit"):
            self.assertIsNotNone(cases[label][1])
        for name, (_, denial) in cases.items():
            if denial:
                with self.subTest(name=name):
                    self.assertTrue(any(denial in v["message"] for v in policy["spec"]["validations"]))

    def test_exec_permission_probe_runs_while_held_and_released(self):
        probe = runpy.run_path(str(ROOT / "workloads/smoke/verify-training-access.py"))
        for released in (False, True):
            seen = set()

            def respond(argv, **kwargs):
                namespace = argv[argv.index("--namespace") + 1]
                verb, resource = argv[3:5]
                if "--subresource=exec" in argv:
                    seen.add((namespace, verb, resource))
                allowed = released and (namespace, verb, resource) == ("training", "create", "jobs.batch")
                return subprocess.CompletedProcess(argv, 0 if allowed else 1, "yes" if allowed else "no", "")

            with patch.object(subprocess, "run", side_effect=respond), patch("builtins.print"):
                probe["check_permissions"](["kubectl"], released)
            self.assertEqual(seen, {(namespace, verb, "pods") for namespace in ("training", "episode-scoring")
                                    for verb in ("get", "create")})

    def test_access_probe_uses_the_released_preflight_entrypoint(self):
        render = runpy.run_path(str(ROOT / "workloads/smoke/verify-training-access.py"))["render_job"]
        trainer = "ghcr.io/fixture/trainer@sha256:" + "1" * 64
        for preflight in ("ghcr.io/fixture/preflight@sha256:" + "2" * 64,
                          "example.invalid/probe@sha256:" + "a" * 64):
            job = render("preflight", trainer, preflight, paired=True)
            container, = job["spec"]["template"]["spec"]["containers"]
            self.assertEqual(container["image"], preflight)
            self.assertEqual(container["command"][0], "/usr/local/bin/python")
            self.assertEqual(container["resources"]["requests"], {"cpu": "1", "memory": "256Mi"})
