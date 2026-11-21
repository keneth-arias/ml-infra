"""Check the worker's publication gate with generations from the paired trainer."""
import json
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from episode_job import verify_fit_result
from opentslm.forecast_checkpoint import finish_direct, publish


class DirectReceiptTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.artifact = self.root / "direct-result.json"
        self.args = Namespace(step=1500, schedule=1500, head="direct", seed=27, run_id="fixture",
                              direct_objective="physical-mae", bundle_sha256="fit", validation_sha256="validation")
        self.spec = {"files": {"data.npz": "data"}, "weights_manifest_sha256": "base"}
        identity = dict(data="data", head="direct", seed=27, schedule=1500, weights="base", logical_run="fixture",
                        direct_objective="physical-mae", input_manifest_sha256="fit", validation_manifest_sha256="validation")
        history = []
        for step, outcome in ((500, "running"), (1000, "early-stopped")):
            history.append(dict(step=step, outcome=outcome, best_step=500, best_mae=1.0))
            stage = self.root / ".saving"
            stage.mkdir()
            (stage / "training.pt.json").write_text(json.dumps({"evaluations": history}))
            publish(self.root, stage, step, identity)
        self.receipt = finish_direct(self.root, identity, 1000, history)

    def verify(self):
        verify_fit_result(self.artifact, self.args, self.spec)

    def test_verified_selected_and_stopping_generations(self):
        self.verify()

    def test_unexpected_receipt_or_digest(self):
        for field, value in (("outcome", "completed"), ("best_mae", 99), ("selected_manifest_sha256", "other"),
                             ("latest_manifest_sha256", "other"), ("best_step", 1001)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.artifact.write_text(json.dumps({**self.receipt, field: value}))
                self.verify()

    def test_wrong_best_pointer(self):
        (self.root / "best").write_text("step-00001000")
        with self.assertRaisesRegex(ValueError, "checkpoint failed verification"):
            self.verify()

    def test_damaged_selected_generation(self):
        (self.root / "step-00000500/training.pt.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checkpoint failed verification"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
