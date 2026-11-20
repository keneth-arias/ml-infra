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
