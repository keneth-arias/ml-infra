"""Verify the artifact chain and CSV for a synthetic pair of completed arms."""
import csv
import hashlib
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


curves = runpy.run_path(str(Path(__file__).resolve().parents[1] / "workloads/sky/direct-learning-curves.py"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DirectCurvesTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        metrics = dict(objective=2, mae=1, high_mae=None, persistence_mae=3,
                       high_persistence_mae=None, windows=2, pairs=12, high_pairs=0,
                       mae_by_horizon=[1] * 6)
        self.history = [dict(step=step, fit_sample=metrics, inner_validation=metrics) for step in (0, 500, 1000)]
        self.meta = self.root / "training.pt.json"
        self.meta.write_text(json.dumps({"evaluations": self.history}))
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"files": {"training.pt.json": {"sha256": digest(self.meta)}}}))
        self.result = dict(self.history[-1], best_step=500, latest_manifest_sha256=digest(self.manifest))
        self.id_fields = dict(seed=27, input_manifest_sha256="fit", validation_manifest_sha256="val")
        self.receipt = self.root / "direct-result.json"
        self.receipt.write_text(json.dumps(self.result))
        self.run = SimpleNamespace(info=SimpleNamespace(status="FINISHED"), data=SimpleNamespace(
            tags={"artifact_sha256": digest(self.receipt), "direct_fit_id": "test"}))
        self.client = Mock()
        self.client.get_run.return_value = self.run
        paths = {"direct-result.json": self.receipt, "evaluations/manifest.json": self.manifest,
                 "evaluations/training.pt.json": self.meta}
        self.client.download_artifacts.side_effect = lambda run_id, name, dest: paths[name]

    def read(self):
        return curves["read_run"](self.client, "test", self.root)

    def record(self, arm, **changes):
        return arm, dict(self.result, identity=dict(self.id_fields, direct_objective=arm, **changes)), self.history

    def test_verified_chain_and_each_damaged_artifact(self):
        self.assertEqual(self.read(), ("test", self.result, self.history))
        for path in (self.receipt, self.manifest, self.meta):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(ValueError):
                    self.read()
                path.write_bytes(original)

    def test_refuse_unfinished_run_and_divergent_evaluation(self):
        self.run.info.status = "RUNNING"
        with self.assertRaisesRegex(ValueError, "finished"):
            self.read()
        self.client.download_artifacts.assert_not_called()
        self.run.info.status = "FINISHED"
        self.receipt.write_text(json.dumps(dict(self.result, step=500)))
        self.run.data.tags["artifact_sha256"] = digest(self.receipt)
        with self.assertRaisesRegex(ValueError, "terminal evaluation"):
            self.read()

    def test_pairing_and_selected_csv_rows(self):
        control = self.record("normalized-mse")
        candidate = self.record("physical-mae")
        for records in ([control], [control, control], [control, self.record("physical-mae", weights="other")]):
            with self.subTest(records=len(records)), self.assertRaises(ValueError):
                curves["export"](records, self.root)
            self.assertFalse((self.root / "curves.csv").exists())
        curves["export"]([candidate, control], self.root)
        with (self.root / "curves.csv").open() as saved:
            data = list(csv.DictReader(saved))
        focus = [(r["step"], r["value"], r["denominator"], r["selected"], r["stopped"])
                 for r in data if r["objective"] == "physical-mae" and r["split"] == "inner_validation"
                 and r["metric"] == "mae_by_horizon_h6" and r["manifest_sha256"] == "val"]
        self.assertEqual(focus, [("0", "1", "2", "False", "False"), ("500", "1", "2", "True", "False"),
                                 ("1000", "1", "2", "False", "True")])
        self.assertEqual((self.root / "curves.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
