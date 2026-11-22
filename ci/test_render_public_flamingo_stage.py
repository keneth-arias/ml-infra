"""Check the offline review packet for the two-leaf public stager."""

from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SKY = Path(__file__).resolve().parents[1] / "workloads" / "sky"
sys.path.insert(0, str(SKY))
render = runpy.run_path(str(SKY / "render-public-flamingo-stage.py"))
build = render["build_packet"]
canonical = render["canonical"]


class PublicStageRenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.promotion = self.directory / "promotion"
        self.binding = self.directory / "binding"
        self.worker = self.directory / "public_flamingo_stage_worker.py"
        self.worker.write_text('print("verified worker ran")\n')
        self.run = "fresh-run"
        self.secret = "stage-fresh-run-inventory"
        self.deploy = "2" * 64
        self.rows = [{
            "name": "base/weights/config.json", "sha256": "1" * 64, "version_id": "1",
            "size": 7, "bucket": "test", "key": "digest/base/weights/config.json",
        }, {"name": "provenance/notice"}]
        self.promotion.write_bytes(canonical({"files": self.rows}))
        self.binding.write_bytes(canonical({
            "cluster_id": render["CLUSTER"], "project_id": "project-test", "phase": "all",
            "bindings": [{"namespace": "training", "claim": "airaware-checkpoints",
                          "size_gib": 128, "pv": "pvc-one", "pvc_uid": "one", "pv_uid": "two",
                          "csi_handle": "computedisk-test", "provider_disk_id": "computedisk-test"}],
        }))
        self.binding.chmod(0o600)
        self.addCleanup(patch.stopall)
        patch.dict(build.__globals__, {
            "PROMOTION_SHA": sha256(self.promotion.read_bytes()).hexdigest(),
            "BINDING_SHA": sha256(self.binding.read_bytes()).hexdigest(),
            "WORKER": self.worker,
            "DEPLOYMENT_SHA": self.deploy,
        }).start()
        self.signed = patch.object(render["release"], "verify_release",
                                   return_value=({"preflight": "signed-preflight"}, self.deploy)).start()

    def packet(self):
        return build(self.promotion, self.binding, self.deploy, self.run, self.secret)

    def test_review_binds_exact_leaves_and_public_objects(self):
        packet = self.packet()
        self.signed.assert_called_once_with(render["release"].TRUST, render["CLUSTER"], render["DEPLOY_COMMIT"])
        spec = packet["job"]["spec"]["template"]["spec"]
        stage, = spec["containers"]
        self.assertEqual(stage["image"], "signed-preflight")
        self.assertEqual(spec["serviceAccountName"], "episode-input-stager")
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertNotIn("fsGroup", spec["securityContext"])
        self.assertEqual(spec["securityContext"]["runAsUser"], 1000)
        self.assertEqual(spec["tolerations"], [
            {"key": "node.kubernetes.io/" + state, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}
            for state in ("not-ready", "unreachable")])
        self.assertEqual(spec["nodeSelector"]["airaware.training/pool"], "training-cpu")
        mounts = {m["mountPath"]: m for m in stage["volumeMounts"]}
        self.assertEqual(set(mounts), {"/base", "/output", "/source", "/program"})
        self.assertEqual(mounts["/base"]["subPath"], "airaware/cache/weights/" + render["WEIGHTS"])
        self.assertEqual(mounts["/output"]["subPath"], "flamingo-baselines/fresh-run")
        self.assertTrue(mounts["/program"]["readOnly"] and mounts["/source"]["readOnly"])
        self.assertTrue(packet["configmap"]["immutable"])
        objects = json.loads(packet["configmap"]["data"]["objects.json"])
        self.assertEqual(objects, [dict(self.rows[0], name="weights/config.json")])
        for item in ("configmap", "job"):
            self.assertEqual(packet[item + "_sha256"], sha256(canonical(packet[item])).hexdigest())

    def test_successor_selects_signed_target_and_refuses_substituted_objects(self):
        digest = "8" * 64
        run = "fsw-" + digest[:32]
        objects = render["public_objects"](self.promotion)
        window = SimpleNamespace(sha256=digest, run=run, deployment_commit="e" * 40,
                                 deployment_sha256=self.deploy, cluster_id=render["CLUSTER"],
                                 project_id="project-test", cpu_node="computeinstance-next",
                                 cpu_provider_id="nebius://computeinstance-next", cpu_node_uid="next-uid",
                                 preflight_image="signed-preflight", preflight_platform_image="platform-image",
                                 scope_sha256="9" * 64, binding_sha256=sha256(self.binding.read_bytes()).hexdigest(),
                                 promotion_sha256=sha256(self.promotion.read_bytes()).hexdigest(),
                                 objects_sha256=sha256(objects).hexdigest(), weights_manifest_sha256=render["WEIGHTS"])
        def packet(w):
            return build(self.promotion, self.binding, self.deploy, run, "stage-" + run + "-inventory", w)

        result = packet(window)
        self.assertEqual(result["window_sha256"], digest)
        self.assertEqual(result["scope_sha256"], window.scope_sha256)
        self.assertEqual(result["preflight_linux_amd64"], window.preflight_platform_image)
        self.assertEqual(result["placement"]["provider_id"], window.cpu_provider_id)
        self.assertIn(window.cpu_node, json.dumps(result["job"]["spec"]["template"]["spec"]["affinity"]))
        self.assertEqual(result["job"]["metadata"]["annotations"], result["configmap"]["metadata"]["annotations"])
        self.assertIn(digest, result["job"]["metadata"]["annotations"].values())
        with self.assertRaises(ValueError):
            packet(None)
        for field in ("preflight_image", "project_id", "objects_sha256", "promotion_sha256", "weights_manifest_sha256"):
            with self.subTest(field=field), patch.object(window, field, "different"), self.assertRaises(ValueError):
                packet(window)

    def test_fails_closed_for_drifted_files_deployment_run_or_secret(self):
        for target in (self.promotion, self.binding):
            original = target.read_bytes()
            target.write_bytes(original + b" ")
            with self.assertRaises(ValueError):
                self.packet()
            target.write_bytes(original)
        self.deploy = "3" * 64
        with self.assertRaises(ValueError):
            self.packet()
        self.deploy = "2" * 64
        self.run = "../qwen"
        with self.assertRaises(ValueError):
            self.packet()
        self.run, self.secret = "fresh-run", "archive-source"
        with self.assertRaises(ValueError):
            self.packet()

    def test_loader_rejects_worker_and_objects_drift_before_execution(self):
        packet = self.packet()
        data = packet["configmap"]["data"]
        stage, = packet["job"]["spec"]["template"]["spec"]["containers"]
        command = [sys.executable, *stage["command"][1:]]
        for filename, content in data.items():
            (self.directory / filename).write_text(content)
        command = [arg.replace("/program/", str(self.directory) + "/") for arg in command]
        ok = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for filename, content in data.items():
            (self.directory / filename).write_text(content + " ")
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("verified worker ran", result.stdout)
            (self.directory / filename).write_text(content)


if __name__ == "__main__":
    unittest.main()
