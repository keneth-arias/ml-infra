import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from episode_release import retain


class ReleaseTest(unittest.TestCase):
    def test_release_survives_rotation_and_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cp = root / "step-00000002"
            cp.mkdir()
            f = cp / "model"
            f.write_bytes(b"model")
            sha = lambda b: hashlib.sha256(b).hexdigest()
            manifest = {
                "step": 2,
                "files": {"model": {"size": 5, "sha256": sha(b"model")}},
            }
            (cp / "manifest.json").write_text(json.dumps(manifest))
            meta = root / "calibration.json"
            meta.write_text("{}")
            m = {
                "version": 1,
                "mode": "concentration",
                "image": "registry/forecast@sha256:" + "a" * 64,
                "checkpoint_sha256": sha((cp / "manifest.json").read_bytes()),
                "base_manifest_sha256": "b" * 64,
                "components": {
                    "calibration.json": sha(b"{}"),
                    "policy.json": sha(b"{}"),
                    "transform.json": sha(b"{}"),
                },
            }
            for name in ("policy.json", "transform.json"):
                (root / name).write_text("{}")
            release = root / "release.json"
            release.write_text(json.dumps(m))
            digest = sha(release.read_bytes())
            out = root / "releases"
            published = retain(release, digest, cp, out)
            f.unlink()
            self.assertEqual((published / cp.name / "model").read_bytes(), b"model")
            self.assertEqual(retain(release, digest, cp, out), published)
            (published / cp.name / "model").write_bytes(b"other")
            with self.assertRaises(ValueError):
                retain(release, digest, cp, out)
