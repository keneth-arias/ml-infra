import runpy
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
HAMI = ROOT / "platform/hami"


class SplitHamiTests(unittest.TestCase):
    def test_scheduler_only_root_points_to_the_exclusive_device_namespace(self):
        objects = list(yaml.safe_load_all(subprocess.check_output(["kubectl", "kustomize", HAMI / "scheduler"])))
        self.assertEqual({(o["kind"], o["metadata"]["name"]) for o in objects}, {
            ("Deployment", "hami-scheduler"), ("ServiceAccount", "hami-scheduler"),
            ("Service", "hami-scheduler"), ("ConfigMap", "hami-scheduler"),
            ("ConfigMap", "hami-scheduler-device"),
        })
        self.assertTrue(all(o["metadata"]["namespace"] == "hami-system" for o in objects))
        deploy, = [o for o in objects if o["kind"] == "Deployment"]
        extender, = [c for c in deploy["spec"]["template"]["spec"]["containers"] if c["name"] == "vgpu-scheduler-extender"]
        self.assertIn("--device-plugin-namespace=hami-device", extender["command"])

    def test_public_pilot_matches_receipt_verifier_and_device_placement(self):
        pilot = yaml.safe_load((HAMI / "workloads/probe-pod.yaml.example").read_text())
        verifier = runpy.run_path(HAMI / "workloads/verify-overlap.py")
        device = next(o for o in yaml.safe_load_all((HAMI / "device/core.yaml").read_text()) if o["kind"] == "DaemonSet")
        self.assertEqual(pilot["spec"]["nodeSelector"], device["spec"]["template"]["spec"]["nodeSelector"])
        self.assertEqual(pilot["spec"]["containers"][0]["image"], verifier["IMAGE"])
        self.assertTrue(verifier["IMAGE"].startswith("docker.io/pytorch/pytorch@sha256:"))
