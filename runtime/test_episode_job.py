import json
import tempfile
import unittest
from pathlib import Path

from episode_job import bundle, sha256


class BundleTest(unittest.TestCase):
    def test_stage_identity_and_exact_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data.npz"
            data.write_bytes(b"fixture")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "phase": "fit",
                        "head": "quantile",
                        "files": {"data.npz": sha256(data)},
                    }
                )
            )
            digest = sha256(manifest)
            self.assertEqual(bundle(root, digest, "fit", "quantile")["phase"], "fit")
            with self.assertRaises(ValueError):
                bundle(root, digest, "infer", "quantile")
            (root / "test-outcomes.npz").write_bytes(b"held-out")
            with self.assertRaises(ValueError):
                bundle(root, digest, "fit", "quantile")
