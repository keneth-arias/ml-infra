import runpy
import unittest
from pathlib import Path

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
        import json
        import subprocess

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
