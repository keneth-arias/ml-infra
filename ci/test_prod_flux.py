"""Verify the deployable Flux source and retained state in the rendered production root."""

import json
import runpy
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

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

    def test_legacy_storage_class_survives_pruning(self):
        rendered = subprocess.check_output(["kubectl", "kustomize", ROOT / "platform/overlays/prod"])
        objects = list(yaml.safe_load_all(rendered))
        storage, = [o for o in objects if o["kind"] == "StorageClass"]
        self.assertEqual(storage["metadata"]["name"], "airaware-network-retain")
        self.assertEqual(storage["reclaimPolicy"], "Retain")
        self.assertEqual(storage["volumeBindingMode"], "WaitForFirstConsumer")
        for obj in objects:
            if obj["kind"] in ("Namespace", "StorageClass"):
                self.assertEqual(obj["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"], "disabled")
        self.assertFalse({"Job", "Pod", "Deployment", "StatefulSet", "PersistentVolumeClaim"}
                         & {o["kind"] for o in objects})

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
            flamingo, denial = cases["flamingo-unreleased-" + mode]
            self.assertIn("--flamingo-binding-sha256", flamingo["command"])
            self.assertEqual(flamingo["image"], images[role])
            self.assertEqual([e["value"] for e in flamingo["env"] if e["name"] == "EPISODE_IMAGE"],
                             [images["trainerImage"]])
            self.assertEqual(denial, "Flamingo Jobs require a signed dual-backbone release.")
            default, denial = cases["default-entrypoint-" + mode]
            self.assertNotIn("command", default)
            self.assertEqual((default["image"], denial), (images[role], "episode entrypoint"))
            for label in ("parallelism", "completions", "backoffLimit", "suspend", "cpu-request",
                          "memory-request", "ephemeral-storage-request"):
                self.assertIsNotNone(cases[label + "-" + mode][1])
        for label in ("gpu-deadline-None", "gpu-deadline-356401", "preflight-memory-limit"):
            self.assertIsNotNone(cases[label][1])
        activation = runpy.run_path(str(ROOT / "bootstrap/nebius/management/native-activation.py"))
        qwen = {role: images[role + "Image"] for role in ("trainer", "preflight")}
        active = [activation["active_policy"](qwen, "a" * 64, stage) for stage in ("support", "gpu")]
        for name, (_, denial) in cases.items():
            if denial:
                with self.subTest(name=name):
                    for variant in active:
                        self.assertTrue(any(denial in v["message"] for v in variant["spec"]["validations"]))

    def test_dual_release_probes_bind_images_to_the_rendered_backbone(self):
        probe = runpy.run_path(str(ROOT / "workloads/smoke/verify-training-access.py"))
        qwen = tuple("ghcr.io/fixture/qwen@sha256:" + pin * 64 for pin in ("1", "2"))
        flamingo = tuple("ghcr.io/fixture/flamingo@sha256:" + pin * 64 for pin in ("3", "4"))
        cases = {}

        def collect(job, name, denial=None):
            self.assertNotIn(name, cases)
            container, = job["spec"]["template"]["spec"]["containers"]
            episode_image, = [e["value"] for e in container["env"] if e["name"] == "EPISODE_IMAGE"]
            cases[name] = (container["image"], episode_image, container.get("command"), denial)

        probe["exercise_release"](*qwen, collect, flamingo_images=flamingo)
        for mode, role in (("job", 0), ("preflight", 1)):
            self.assertEqual(cases[f"flamingo-default-entrypoint-{mode}"],
                             (flamingo[role], flamingo[0], None, "approved image"))
            for backbone, images, other in (("qwen", qwen, flamingo), ("flamingo", flamingo, qwen)):
                valid = ("paired-" if backbone == "qwen" else "flamingo-") + mode
                container, trainer, cmd, denial = cases[valid]
                self.assertEqual((container, trainer, denial), (images[role], images[0], None))
                self.assertEqual(cmd[2:4], ["fit", "direct"])
                self.assertEqual("--preflight" in cmd, mode == "preflight")
                if backbone == "flamingo":
                    binding = cmd.index("--flamingo-binding-sha256")
                    flags = cmd[binding:binding + 7]
                    self.assertEqual([flags[0], flags[2], flags[5]],
                                     ["--flamingo-binding-sha256", "--flamingo-baselines", "--flamingo-parity-sha256"])
                    for pin in (flags[1], flags[3], flags[4], flags[6]):
                        self.assertRegex(pin, "^[0-9a-f]{64}$")
                    self.assertNotEqual(flags[3], flags[4])
                else:
                    self.assertNotIn("--flamingo-binding-sha256", cmd)
                self.assertEqual(cases[f"{backbone}-wrong-backbone-{mode}"],
                                 (other[role], images[0], cmd, "approved image"))
                self.assertEqual(cases[f"{backbone}-wrong-episode-image-{mode}"],
                                 (images[role], other[0], cmd, "approved environment"))
                self.assertEqual(cases[f"{backbone}-wrong-pair-{mode}"],
                                 (other[role], other[0], cmd, "approved image"))

    def test_activation_probe_covers_every_signed_training_role(self):
        activation = runpy.run_path(str(ROOT / "bootstrap/nebius/management/native-activation.py"))
        probe = runpy.run_path(str(ROOT / "workloads/smoke/verify-training-access.py"))["main"]
        tx = activation["Transaction"](None, None, None, Path("/fixture"), None)
        refs = {role: f"ghcr.io/fixture/{role}@sha256:" + str(i) * 64 for i, role in
                enumerate(("rootless-cpu", "trainer", "preflight", "flamingo-trainer", "flamingo-preflight"))}
        qwen = {k: v for k, v in refs.items() if not k.startswith("flamingo-")}
        for signed, supplied in ((qwen, qwen), (refs, refs), (refs, qwen),
                                 (refs, refs | {"flamingo-trainer": refs["trainer"]})):
            with self.subTest(signed=signed.keys(), supplied=supplied):
                with patch.object(subprocess, "run") as launch:
                    tx.probe(supplied)
                command, = launch.call_args.args
                self.assertEqual(command[:2], [sys.executable, str(ROOT / "workloads/smoke/verify-training-access.py")])
                backend, exercise = Mock(), Mock()
                with (patch.object(sys, "argv", command[1:]),
                      patch.dict(probe.__globals__, release_images=lambda: signed, kubectl_client=backend,
                                 check_permissions=Mock(), exercise_release=exercise)):
                    if signed == supplied:
                        probe()
                        flamingo = (refs["flamingo-trainer"], refs["flamingo-preflight"]) if signed == refs else None
                        self.assertEqual(exercise.call_args.args[:2], (refs["trainer"], refs["preflight"]))
                        self.assertEqual(exercise.call_args.kwargs, {"flamingo_images": flamingo})
                    else:
                        with self.assertRaises(ValueError):
                            probe()
                        backend.assert_not_called()
                        exercise.assert_not_called()

    def test_exec_permission_probe_runs_while_held_and_released(self):
        probe = runpy.run_path(str(ROOT / "workloads/smoke/verify-training-access.py"))
        for released in (False, True):
            seen = set()

            def respond(argv, **kwargs):
                namespace = argv[argv.index("--namespace") + 1]
                verb, resource = argv[3:5]
                if "--subresource=exec" in argv:
                    seen.add((namespace, verb, resource))
                rule = (verb, resource, next((arg for arg in argv if arg.startswith("--subresource=")), ""))
                allowed = released and namespace == "training" and rule in {
                    ("create", "jobs.batch", ""), ("get", "jobs.batch", ""),
                    ("list", "jobs.batch", ""), ("watch", "jobs.batch", ""),
                    ("get", "pods", ""), ("list", "pods", ""), ("get", "pods", "--subresource=log"),
                }
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
