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

    def test_future_fields_and_metric_names(self):
        from episode_job import verify_fields, training_metrics

        with self.assertRaisesRegex(ValueError, "phase"):
            verify_fields({"window_id", "history", "targets"}, "infer", "direct")
        result = training_metrics(
            {
                "mae": 2.0,
                "width_by_horizon": [9.0, 8.0],
                "window_ids": ["secret"],
                "step": 7,
            }
        )
        self.assertEqual(result, {"mae": 2.0, "width_h1": 9.0, "width_h2": 8.0})

    def test_matched_fit_reuses_exact_quantile_input(self):
        from episode_job import prepare_point_control

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "quantile"
            source.mkdir()
            (source / "data.npz").write_bytes(b"same ordered archive")
            spec = {
                "phase": "fit",
                "head": "quantile",
                "block": "train",
                "schema": "forecast-input-v1",
                "unit": "ug/m3",
                "split_sha256": "1" * 64,
                "weights_manifest_sha256": "2" * 64,
                "files": {"data.npz": sha256(source / "data.npz")},
            }
            (source / "manifest.json").write_text(json.dumps(spec))
            expected = sha256(source / "manifest.json")
            with self.assertRaises(ValueError):
                prepare_point_control(source, "0" * 64, root / "bad")
            out = root / "control"
            got = prepare_point_control(source, expected, out)
            bound = bundle(out, got, "fit", "direct")
            self.assertEqual(bound["point_control_of"], expected)
            self.assertEqual(bound["files"], spec["files"])
            self.assertEqual(
                (out / "data.npz").read_bytes(), (source / "data.npz").read_bytes()
            )
