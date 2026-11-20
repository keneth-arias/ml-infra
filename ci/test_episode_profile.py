import json
import runpy
import subprocess
import unittest
from pathlib import Path

import yaml

profile = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py")
)


class ProfileTest(unittest.TestCase):
    def test_only_phase_inputs_are_visible(self):
        for phase in ("fit", "infer"):
            spec = profile["pod_spec"](phase, "demo")
            c = spec["containers"][0]
            mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
            self.assertTrue(mounts["/base"]["readOnly"])
            self.assertEqual(
                mounts["/bundle"]["subPath"], "airaware/episodes/demo/" + phase
            )
            self.assertTrue(mounts["/bundle"]["readOnly"])
            self.assertNotIn("/opt/airaware", mounts)
            self.assertEqual(
                set(mounts) & {"/state", "/checkpoint"},
                {"/state"} if phase == "fit" else {"/checkpoint"},
            )
            if phase == "infer":
                self.assertTrue(mounts["/checkpoint"]["readOnly"])

    def test_preflight_only_mounts_inputs_and_never_requests_gpu(self):
        script = (
            Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py"
        )
        for phase in ("fit", "infer"):
            cmd = [
                "python3",
                str(script),
                "preflight",
                phase,
                "quantile",
                "demo",
                "--image",
                "example.com/test@sha256:" + "a" * 64,
                "--bundle-sha256",
                "b" * 64,
                "--step",
                "1",
            ]
            if phase == "fit":
                cmd.extend(["--schedule", "1"])
            job = json.loads(subprocess.check_output(cmd))
            pod = job["spec"]["template"]["spec"]
            c = pod["containers"][0]
            self.assertNotIn("runtimeClassName", pod)
            self.assertEqual(c["resources"]["requests"]["memory"], "1G")
            self.assertNotIn("memory", c["resources"].get("limits", {}))
            self.assertFalse(
                any("nvidia.com/gpu" in v for v in c["resources"].values())
            )
            self.assertEqual(
                {m["mountPath"] for m in c["volumeMounts"]},
                {"/base", "/bundle"}
                if phase == "fit"
                else {"/base", "/bundle", "/checkpoint"},
            )
            self.assertTrue(all(m["readOnly"] for m in c["volumeMounts"]))
            self.assertIn("--preflight", c["command"])

    def test_training_requests_memory_without_a_container_ceiling(self):
        workloads = Path(__file__).resolve().parents[1] / "workloads"
        for phase in ("fit", "infer"):
            for fmt in ("job", "sky"):
                args = [
                    "python3",
                    str(workloads / "sky/episode-profile.py"),
                    fmt,
                    phase,
                    "quantile",
                    "demo",
                    "--image",
                    "registry/test@sha256:" + "a" * 64,
                    "--bundle-sha256",
                    "b" * 64,
                    "--step",
                    "1",
                ]
                if phase == "fit":
                    args.extend(["--schedule", "1"])
                result = json.loads(subprocess.check_output(args))
                if fmt == "job":
                    spec = result["spec"]["template"]["spec"]
                    self.assertEqual(
                        spec["containers"][0]["resources"]["limits"],
                        {"nvidia.com/gpu": 1},
                    )
                else:
                    config = result["config"]["kubernetes"]
                    self.assertIs(config["set_pod_resource_limits"], False)
                    self.assertEqual(result["resources"]["accelerators"], "rtx5070ti:1")
                    spec = config["pod_config"]["spec"]
                resources = spec["containers"][0]["resources"]
                self.assertEqual(resources["requests"]["memory"], "12G")
                self.assertNotIn("memory", resources.get("limits", {}))

        for name in ("dual-gpu", "fsdp"):
            job = yaml.safe_load((workloads / "smoke" / f"{name}.yaml").read_text())
            resources = job["spec"]["template"]["spec"]["containers"][0]["resources"]
            self.assertIn("memory", resources["requests"])
            self.assertEqual(resources["limits"]["nvidia.com/gpu"], 2)
            self.assertNotIn("memory", resources["limits"])
        for name in ("airaware", "lora"):
            task = yaml.safe_load((workloads / "sky" / f"{name}.yaml").read_text())
            self.assertIs(
                task["config"]["kubernetes"]["set_pod_resource_limits"], False
            )
            self.assertGreater(task["resources"]["memory"], 0)
