import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from qwen import CHANNELS, CHECKPOINT, HORIZONS, MLX_WEIGHTS, MODEL, SCALER, digest, export, predict_mlx, write_joint_history


class ExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.window = {
            "origin": "2026-11-23T01:00:00", "history": [12] * 24,
            "observed": [14] + [None] * 5, "forecast": None,
        }
        self.missing = {
            "origin": "2026-11-23T00:00:00", "history": [None] + [12] * 23,
            "observed": [12] + [None] * 5, "forecast": None,
        }
        self.snapshot = {
            "schema_version": 1,
            "stations": {"SAN_BORJA": {"windows": [self.missing, self.window]}},
        }
        (self.root / "capture.json").write_text(json.dumps(self.snapshot))
        self.ids = ["SAN_BORJA/" + self.window["origin"]]
        np.savez(self.root / "history.npz", window_id=self.ids,
                 history=np.array([self.window["history"]], dtype="float32"))

    def write_forecast(self, *, checkpoint=CHECKPOINT, ids=None, view="first", scaler=SCALER):
        np.savez(self.root / "predictions.npz", window_id=self.ids if ids is None else ids,
                 predicted=np.array([[[point, 1000] for point in range(8, 14)]]),
                 forecast_hour=HORIZONS, checkpoint_sha256=checkpoint, view=view,
                 scaler_sha256=scaler, channels=CHANNELS)
        (self.root / "qwen-receipt.json").write_text(json.dumps({
            "history_sha256": digest(self.root / "history.npz"),
            "output_sha256": digest(self.root / "predictions.npz"),
        }))

    def test_export_preserves_observations_and_missing_history(self):
        self.write_forecast()
        export(self.root)
        result = json.loads((self.root / "actual.json").read_bytes())
        self.assertEqual(result["model"]["name"], MODEL)
        self.assertEqual(result["model"]["checkpoint_sha256"], CHECKPOINT)
        self.window["forecast"] = [8, 9, 10, 11, 12, 13]
        self.assertEqual(result["stations"], self.snapshot["stations"])

    def test_rejects_wrong_checkpoint_or_history_ids(self):
        for fields in ({"checkpoint": "a" * 64}, {"ids": ["SAN_BORJA/other"]},
                       {"view": "both"}, {"scaler": "a" * 64}):
            with self.subTest(fields=fields):
                self.write_forecast(**fields)
                with self.assertRaisesRegex(ValueError, "Predicciones incompatibles"):
                    export(self.root)
                self.assertFalse((self.root / "actual.json").exists())

    def test_rejects_changed_prediction_bytes(self):
        self.write_forecast()
        with (self.root / "predictions.npz").open("ab") as output:
            output.write(b"changed")
        with self.assertRaisesRegex(ValueError, "recibo de inferencia"):
            export(self.root)

    def test_joint_history_has_no_targets_and_masks_pm10(self):
        joint = self.root / "joint.npz"
        write_joint_history(self.root / "history.npz", joint)
        with np.load(joint, allow_pickle=False) as data:
            self.assertEqual(set(data.files), {"window_id", "history", "channels"})
            np.testing.assert_array_equal(data["window_id"], self.ids)
            np.testing.assert_array_equal(data["channels"], ["pm25", "pm10"])
            np.testing.assert_array_equal(data["history"][0, :, 0], self.window["history"])
            np.testing.assert_array_equal(data["history"][0, :, 1], np.zeros(24))

    def test_mlx_publishes_pm25_only_in_capture_order(self):
        forecast = [8, 9, 10, 11, 12, 13]

        def response(request):
            self.assertEqual(request.pop("histories"), [self.window["history"]])
            self.assertEqual(request["window_ids"], self.ids)
            return {**request, "forecast": [forecast]}

        with patch("qwen.mlx_request", side_effect=response):
            predict_mlx(self.root)
        export(self.root)
        self.window["forecast"] = forecast
        self.assertEqual(json.loads((self.root / "actual.json").read_text())["stations"], self.snapshot["stations"])

    def test_mlx_rejects_wrong_weights_or_window_order(self):
        for fields in ({"weights_sha256": "1" * 64}, {"window_ids": ["wrong"]}):
            with self.subTest(fields=fields), patch(
                "qwen.mlx_request", side_effect=lambda request: {
                    **{key: value for key, value in request.items() if key != "histories"},
                    "forecast": [[1] * 6], **fields,
                },
            ):
                with self.assertRaisesRegex(ValueError, "identidad de la solicitud"):
                    predict_mlx(self.root)
                self.assertFalse((self.root / "predictions.npz").exists())

    def test_export_rejects_mlx_receipt_for_different_weights(self):
        self.write_forecast()
        receipt_path = self.root / "qwen-receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt_path.write_text(json.dumps({**receipt, "backend": "mlx-metal", "weights_sha256": MLX_WEIGHTS[:-1] + "0"}))
        with self.assertRaisesRegex(ValueError, "recibo de inferencia"):
            export(self.root)


if __name__ == "__main__":
    unittest.main()
