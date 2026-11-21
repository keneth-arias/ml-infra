import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "workloads/sky"))
from stage_episode_inputs import publish
from versioned_episode_inputs import ObjectSet
from production_target import require_sky


class EpisodeRolloutTest(unittest.TestCase):
    def test_render_contract(self):
        options = [
            "fit",
            "episode",
            "trial-1",
            "--image",
            "trainer@sha256:" + "a" * 64,
            "--preflight-image",
            "cpu@sha256:" + "b" * 64,
            "--bundle-sha256",
            "c" * 64,
            "--cached-weights-sha256",
            "d" * 64,
            "--step",
            "4",
            "--schedule",
            "4",
            "--graceful-stop",
        ]
        for mode in ("preflight", "job", "sky"):
            doc = json.loads(
                subprocess.check_output(
                    [
                        sys.executable,
                        ROOT / "workloads/sky/episode-profile.py",
                        mode,
                        *options,
                    ]
                )
            )
            pod = (
                doc["config"]["kubernetes"]["pod_config"]["spec"]
                if mode == "sky"
                else doc["spec"]["template"]["spec"]
            )
            c = pod["containers"][0]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(
                [m["subPath"] for m in c["volumeMounts"][:2]],
                [
                    "airaware/cache/bundles/" + "c" * 64,
                    "airaware/cache/weights/" + "d" * 64,
                ],
            )
            if mode == "preflight":
                self.assertEqual(c["command"][0], "/usr/local/bin/python")
                self.assertEqual(c["image"], "cpu@sha256:" + "b" * 64)
                self.assertNotIn("--graceful-stop", c["command"])
                self.assertEqual(c["resources"]["limits"]["memory"], "512Mi")
            else:
                self.assertEqual(pod["terminationGracePeriodSeconds"], 90)
                self.assertIn(
                    "--graceful-stop", doc["run"] if mode == "sky" else c["command"]
                )

    def test_partial_copy_and_immutable_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "src"
            src.mkdir()
            (src / "a").write_bytes(b"ok")
            good = hashlib.sha256(b"ok").hexdigest()

            def verify(tree):
                if hashlib.sha256((tree / "a").read_bytes()).hexdigest() != good:
                    raise ValueError("corrupt")

            cache = root / "cache"
            (src / "unsafe").symlink_to(src / "a")
            with self.assertRaises(ValueError):
                publish(cache, good, src, verify)
            self.assertFalse((cache / good).exists())
            (src / "unsafe").unlink()
            self.assertEqual(publish(cache, good, src, verify)["cache"], "miss")
            self.assertEqual(publish(cache, good, src, verify)["cache"], "hit")
            (cache / good / "a").write_bytes(b"bad")
            with self.assertRaises(ValueError):
                publish(cache, good, src, verify)

    def test_versioned_objects(self):
        import io

        prefix = "https://storage.eu-south1.nebius.cloud/bucket/approved/"
        row = {
            "name": "data.npz",
            "size": 2,
            "version_id": "123",
            "url": prefix + "fit/data.npz?versionId=123",
        }
        for override in (
            {"name": "../x"},
            {"size": -1},
            {"version_id": "other"},
            {"url": prefix.replace("approved/", "elsewhere/") + "x?versionId=123"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                ObjectSet([{**row, **override}], prefix, False)
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("versioned_episode_inputs.build_opener") as opener,
        ):
            response = io.BytesIO(b"ok")
            response.headers = {"Content-Length": "2", "x-amz-version-id": "123"}
            opener.return_value.open.return_value = response
            target = Path(temp) / "dst"
            ObjectSet([row], prefix, False).copy(target)
            self.assertEqual((target / "data.npz").read_bytes(), b"ok")

    def test_wrong_endpoint_fails_before_sky(self):
        with (
            patch.dict(
                "os.environ",
                {
                    "NEBIUS_CPU_SKY_URL": "https://prod.example",
                    "SKYPILOT_API_SERVER_ENDPOINT": "http://127.0.0.1:46580",
                },
            ),
            patch("production_target.subprocess.check_output") as sky,
        ):
            with self.assertRaises(ValueError):
                require_sky("sky-cpu")
            sky.assert_not_called()
