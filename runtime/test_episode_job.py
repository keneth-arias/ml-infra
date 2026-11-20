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

    def test_retry_accepts_only_bound_complete_result(self):
        from episode_job import stage_attempt

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifact = root / "result"

            def verify():
                if artifact.read_bytes() != b"ok":
                    raise ValueError("damaged result")

            with self.assertRaisesRegex(RuntimeError, "network"):
                with stage_attempt(
                    root, "infer-2", {"source": "a"}, artifact, verify
                ) as reuse:
                    self.assertFalse(reuse)
                    artifact.write_bytes(b"ok")
                    raise RuntimeError("network")
            with stage_attempt(
                root, "infer-2", {"source": "a"}, artifact, verify
            ) as reuse:
                self.assertTrue(reuse)
            with (
                self.assertRaises(ValueError),
                stage_attempt(root, "infer-2", {"source": "b"}, artifact, verify),
            ):
                self.fail("changed identity")
            artifact.write_bytes(b"bad")
            with (
                self.assertRaises(ValueError),
                stage_attempt(root, "infer-2", {"source": "a"}, artifact, verify),
            ):
                self.fail("damaged artifact")

    def test_unlisted_weight_file_is_rejected(self):
        from episode_job import verify_base

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weights = root / "weights"
            weights.mkdir()
            (weights / "config.json").write_text("{}")
            sums = root / "SHA256SUMS"
            sums.write_text(sha256(weights / "config.json") + "  weights/config.json\n")
            verify_base(weights, sha256(sums))
            (weights / "weights.bin").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "Unlisted"):
                verify_base(weights, sha256(sums))
