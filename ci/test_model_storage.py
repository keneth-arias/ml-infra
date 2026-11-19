"""Verify that model jobs retain writable state separately from frozen inputs."""

import unittest
from pathlib import Path
from runpy import run_path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class ModelStorageTest(unittest.TestCase):
    def test_new_profile_keeps_pilot_paths_and_pins_the_image(self):
        profile = ROOT / "workloads/sky/airaware.yaml"
        original = yaml.safe_load(profile.read_text())
        render = run_path(str(profile.with_name("model-state.py")))["model_task"]
        task = render(profile)
        self.assertEqual(task["resources"]["image_id"], original["resources"]["image_id"])
        spec = task["config"]["kubernetes"]["pod_config"]["spec"]
        claims = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in spec["volumes"] if "persistentVolumeClaim" in v}
        mounts = {m["mountPath"]:m for m in spec["containers"][0]["volumeMounts"]}
        self.assertEqual(claims[mounts["/inputs"]["name"]], "airaware-checkpoints")
        self.assertTrue(mounts["/inputs"]["readOnly"])
        self.assertEqual(claims[mounts["/checkpoints"]["name"]], "airaware-model-state")
        self.assertNotIn("subPath", mounts["/checkpoints"])
        self.assertEqual(yaml.safe_load(profile.read_text()), original)
