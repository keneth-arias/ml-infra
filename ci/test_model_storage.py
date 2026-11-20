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

    def test_joint_pair_mounts_only_versioned_historical_inputs_and_base_weights(self):
        profile=ROOT/"workloads/sky/airaware.yaml"
        render=run_path(str(profile.with_name("model-state.py")))["model_task"]
        task=render(profile, joint=True)
        spec=task["config"]["kubernetes"]["pod_config"]["spec"]
        mounts={m["mountPath"]:m for m in spec["containers"][0]["volumeMounts"]}
        self.assertEqual(mounts["/inputs"]["subPath"],"airaware/joint-inputs")
        self.assertEqual(mounts["/inputs/qwen"]["subPath"],"airaware/inputs/qwen")
        self.assertTrue(all(mounts[k]["readOnly"] for k in ("/inputs","/inputs/qwen")))
        self.assertIn("@sha256:",task["resources"]["image_id"])
        self.assertEqual(task["envs"]["LORA"],"1")

    def test_joint_limits_host_memory_and_mounts_capped_worker(self):
        profile = ROOT / "workloads/sky/airaware.yaml"
        task = run_path(str(profile.with_name("model-state.py")))["model_task"](profile, joint=True)
        spec = task["config"]["kubernetes"]["pod_config"]["spec"]
        container, = spec["containers"]
        self.assertEqual(container["resources"]["limits"]["memory"], "55G")
        mount, = [m for m in container["volumeMounts"] if m["mountPath"] == "/opt/airaware"]
        volume, = [v for v in spec["volumes"] if v["name"] == mount["name"]]
        self.assertEqual(volume["configMap"]["name"], "airaware-workers-v2")
        self.assertIn("/opt/airaware/airaware.py", task["run"])
