"""Check the worker's publication gate with generations from the paired trainer."""
import fcntl
import json
import sys
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import MagicMock, Mock, patch

import episode_job
from episode_job import sha256, verify_fit_result, verify_resume_checkpoint
from opentslm.forecast_checkpoint import files, finish_direct, publish


class DirectReceiptTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.artifact = self.root / "direct-result.json"
        self.args = Namespace(step=1500, schedule=1500, head="direct", seed=27, run_id="fixture",
                              direct_objective="physical-mae", bundle_sha256="a" * 64, validation_sha256="b" * 64)
        self.spec = {"files": {"data.npz": "data"}, "weights_manifest_sha256": "base"}
        self.identity = dict(data="data", head="direct", seed=27, schedule=1500, weights="base", logical_run="fixture",
                             direct_objective="physical-mae", input_manifest_sha256=self.args.bundle_sha256,
                             validation_manifest_sha256=self.args.validation_sha256, recipe_sha256="e" * 64)

    def create_running(self, *, flamingo=False):
        if flamingo:
            self.identity.update(weights=episode_job.FLAMINGO_WEIGHTS, backbone="flamingo", binding_sha256="c" * 64)
        self.history = [dict(step=500, outcome="running", best_step=500, best_mae=1.0, next_eval_step=1000, patience=0)]
        self.save(500)

    def finish(self):
        self.history.append(dict(step=1000, outcome="early-stopped", best_step=500, best_mae=1.0,
                                 next_eval_step=None, patience=0))
        self.save(1000)
        self.receipt = finish_direct(self.root, self.identity, 1000, self.history)

    def create_terminal(self, *, flamingo=False):
        self.create_running(flamingo=flamingo)
        self.finish()

    def save(self, step):
        stage = self.root / ".saving"
        (stage / "dcp").mkdir(parents=True)
        (stage / "dcp/__0_0.distcp").write_bytes(f"adapter and optimizer at {step}".encode())
        (stage / "training.pt.json").write_text(json.dumps({"evaluations": self.history, "scheduler": {}}))
        return publish(self.root, stage, step, self.identity)[0]

    def pin(self, step):
        return f'{step}:{sha256(self.root / f"step-{step:08d}" / "manifest.json")}'

    def verify(self):
        verify_fit_result(self.artifact, self.args, self.spec)

    def test_verified_selected_and_stopping_generations(self):
        self.create_terminal()
        self.verify()

    def test_unexpected_receipt_or_digest(self):
        self.create_terminal()
        for field, value in (("outcome", "completed"), ("best_mae", 99), ("selected_manifest_sha256", "other"),
                             ("latest_manifest_sha256", "other"), ("best_step", 1001)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.artifact.write_text(json.dumps({**self.receipt, field: value}))
                self.verify()

    def test_funded_resume_matches_the_recoverable_generation(self):
        self.create_terminal()
        self.args.resume_checkpoint = self.pin(1000)
        verify_resume_checkpoint(self.root, self.args, self.spec)
        self.args.resume_checkpoint = self.pin(500)
        with self.assertRaisesRegex(ValueError, "different generation"):
            verify_resume_checkpoint(self.root, self.args, self.spec)
        self.args.resume_checkpoint = self.pin(1000)
        (self.root / "step-00000500/training.pt.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "different generation"):
            verify_resume_checkpoint(self.root, self.args, self.spec)
        self.args.resume_checkpoint = "1000:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            verify_resume_checkpoint(self.root, self.args, self.spec)

    def test_wrong_best_pointer(self):
        self.create_terminal()
        (self.root / "best").write_text("step-00001000")
        with self.assertRaisesRegex(ValueError, "checkpoint failed verification"):
            self.verify()

    def test_damaged_selected_generation(self):
        self.create_terminal()
        (self.root / "step-00000500/training.pt.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "checkpoint failed verification"):
            self.verify()

    def run_worker(self, pin, *, reused=True, flamingo=True, error=None, launch=None):
        args = self.args
        fit_spec = {**self.spec, "weights_manifest_sha256": episode_job.COHORT_WEIGHTS} if flamingo else self.spec
        image = "fixture-image@sha256:" + "f" * 64
        binding = "c" * 64
        argv = ["episode_job.py", "fit", "direct", "--direct-objective", args.direct_objective,
                "--bundle-sha256", args.bundle_sha256, "--validation-sha256", args.validation_sha256,
                "--run-id", args.run_id, "--step", str(args.step), "--schedule", str(args.schedule),
                "--resume", "--resume-checkpoint", pin]
        intent = dict(self.args.__dict__, phase="fit", batch=16, image=image)
        if flamingo:
            baselines, parity = ["d" * 64, "e" * 64], "f" * 64
            argv += ["--flamingo-binding-sha256", binding, "--flamingo-baselines", *baselines,
                     "--flamingo-parity-sha256", parity]
            intent.update(flamingo_binding_sha256=binding, flamingo_baselines=baselines,
                          flamingo_parity_sha256=parity)
        episode_job.bind_file(self.root / ".fit-step-00001500.intent.json", intent)
        receipt = self.root / ".fit-step-00001500.receipt.json"

        def verify_under_lock(path):
            with (self.root / ".episode-worker.lock").open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return files(path)

        cohort = {"window_id": range(args.schedule * 16)}
        inputs = ModuleType("opentslm.forecast_inputs")
        inputs.direct_cohorts = Mock(return_value=(cohort, cohort))
        mlflow = MagicMock()
        prior = [Mock(info=Mock(status="FINISHED"), data=Mock(tags={"artifact_sha256": sha256(self.artifact)}))] if reused else []
        mlflow.MlflowClient.return_value.search_runs.return_value = prior
        paths = {"/state": self.root, "/bundle": self.root}
        with (
            patch.object(sys, "argv", argv),
            patch.dict(sys.modules, {"numpy": ModuleType("numpy"), "mlflow": mlflow, "opentslm.forecast_inputs": inputs}),
            patch.dict(episode_job.os.environ, {"EPISODE_IMAGE": image, "INFRA_SOURCE_REVISION": "infra",
                                              "FORECAST_SOURCE_REVISION": "trainer", "AIRAWARE_SOURCE_REVISION": "airaware"}),
            patch.object(episode_job, "Path", side_effect=lambda path: paths.get(path, Path(path))),
            patch.object(episode_job, "bundle_manifest", return_value=fit_spec),
            patch.object(episode_job, "verify_base"),
            patch.object(episode_job, "flamingo_binding", return_value=b"{}"),
            patch.object(episode_job, "validated_baselines", return_value=[]),
            patch.object(episode_job, "verify_flamingo_selection", return_value=None) as probe,
            patch.object(episode_job.subprocess, "Popen", side_effect=launch) as trainer,
            patch("opentslm.forecast_checkpoint.files", side_effect=verify_under_lock) as hashed,
        ):
            if error:
                with self.assertRaisesRegex(ValueError, error):
                    episode_job.main()
                probe.assert_not_called()
                trainer.assert_not_called()
                mlflow.start_run.assert_not_called()
                self.assertFalse(receipt.exists())
            else:
                episode_job.main()
                self.assertEqual(probe.call_count, int(flamingo))
                self.assertEqual(trainer.call_count, int(not reused))
                self.assertEqual(json.loads(receipt.read_bytes()), sha256(self.artifact))
                if reused:
                    self.assertEqual([call.args[0].name for call in hashed.call_args_list],
                                     ["step-00001000", "step-00000500"])

    def test_reused_flamingo_receipt_checks_pin_before_probes(self):
        self.create_terminal(flamingo=True)
        self.run_worker("1000:" + "0" * 64, error="manifest differs")
        self.run_worker(self.pin(500), error="different generation")
        self.run_worker(self.pin(1000))
        self.run_worker(self.pin(1000))

    def test_reused_receipt_rejects_stale_terminal_for_same_identity(self):
        self.create_terminal(flamingo=True)
        self.history[-1] = {**self.history[-1], "outcome": "running", "next_eval_step": 1500}
        self.save(1250)
        self.run_worker(self.pin(1250), error="does not match the funded checkpoint")

    def test_reused_receipt_rejects_selected_checkpoint_damage(self):
        self.create_terminal(flamingo=True)
        (self.root / "step-00000500/dcp/__0_0.distcp").write_bytes(b"damaged selected state")
        self.run_worker(self.pin(1000), error="different generation")

    def test_reused_receipt_rejects_stale_latest_digest_at_funded_step(self):
        self.create_terminal(flamingo=True)
        self.artifact.write_text(json.dumps({**self.receipt, "latest_manifest_sha256": "0" * 64}))
        self.run_worker(self.pin(1000), error="checkpoint failed verification")

    def test_reused_qwen_receipt_allows_partial_later_checkpoint(self):
        self.create_terminal()
        (self.root / "step-00001250/dcp").mkdir(parents=True)
        self.run_worker(self.pin(1000), flamingo=False)

    def test_resume_without_receipt_checks_pin_before_trainer_launch(self):
        self.create_running(flamingo=True)
        self.run_worker("500:" + "0" * 64, reused=False, error="manifest differs")

        def launch(*args, **kwargs):
            self.finish()
            context = MagicMock()
            context.__enter__.return_value = Mock(stdout=[], wait=Mock(return_value=0))
            return context

        self.run_worker(self.pin(500), reused=False, launch=launch)


if __name__ == "__main__":
    unittest.main()
