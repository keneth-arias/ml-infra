"""Verify the deployable Flux source and retained state in the rendered production root."""

import json
from pathlib import Path
import subprocess
import sys
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "bootstrap/nebius/management/render-flux-source.py"


class ProductionFluxTests(unittest.TestCase):
    def test_reconciliation_uses_published_shape_and_preserves_state(self):
        head = subprocess.check_output(["git", "-C", ROOT, "rev-parse", "HEAD"], text=True).strip()
        for suspended in (True, False):
            with self.subTest(suspended=suspended):
                cmd = [sys.executable, RENDER, head]
                if suspended:
                    cmd.append("--suspend")
                source, reconcile = json.loads(subprocess.check_output(cmd))["items"]
                self.assertEqual(source["spec"]["ref"], {"commit": head})
                self.assertEqual(source["spec"]["secretRef"]["name"], "nebius-git-read")
                self.assertEqual(reconcile["spec"]["sourceRef"]["name"], source["metadata"]["name"])
                self.assertEqual(reconcile["spec"]["decryption"]["secretRef"]["name"], "nebius-sops-age")
                self.assertEqual(reconcile["spec"]["path"], "./clusters/prod-nebius")
                self.assertEqual(reconcile["spec"]["deletionPolicy"], "Orphan")
                self.assertTrue(reconcile["spec"]["prune"])
                self.assertFalse(reconcile["spec"]["wait"])
                for obj in (source, reconcile):
                    self.assertIs(obj["spec"]["suspend"], suspended)
        for bad in ("main", "0" * 40):
            self.assertNotEqual(subprocess.run([sys.executable, RENDER, bad], capture_output=True).returncode, 0)

    def test_claims_bind_on_first_consumer_and_survive_pruning(self):
        rendered = subprocess.check_output(["kubectl", "kustomize", ROOT / "clusters/prod-nebius"])
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
