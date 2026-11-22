"""Test the attended checkpoint reader and host archive verifier with synthetic bytes."""

import hashlib
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from test_gpu_plan import gpu_window, timings, verified_selection


ROOT = Path(__file__).resolve().parents[1] / "bootstrap/nebius/management/checkpoint-reader"
READER = runpy.run_path(str(ROOT / "read-checkpoint.py"))
ARCHIVE = runpy.run_path(str(ROOT / "verify-checkpoint-archive.py"))
POD_LABELS = runpy.run_path(str(ROOT / "verify-pod-labels.py"))

IDENTITY = {
    "logical_run": "synthetic-control", "seed": 27, "head": "direct", "direct_objective": "normalized-mse",
    "schedule": 500, "input_manifest_sha256": "a" * 64, "validation_manifest_sha256": "b" * 64, "weights": "c" * 64,
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(root):
    return {str(path.relative_to(root)): {"size": path.stat().st_size, "sha256": digest(path)}
            for path in root.rglob("*") if path.is_file() and path.name != "manifest.json"}


def manifest(root):
    return json.loads((root / "manifest.json").read_bytes())


def dependencies(path, identity):
    info = manifest(path)
    return {path} if info["identity"] == identity and info["files"] == files(path) else set()


CHECKPOINT = SimpleNamespace(sha256=digest, files=files, read_manifest=manifest, direct_dependencies=dependencies)


def make_generation(root, step, *, terminal=False):
    path = root / f"step-{step:08d}"
    path.mkdir()
    (path / "model").write_bytes(b"synthetic payload")
    event = {"step": step, "best_step": step, "outcome": "completed" if terminal else "running"}
    (path / "training.pt.json").write_text(json.dumps({"evaluations": [event]}))
    (path / "manifest.json").write_text(json.dumps({"step": step, "identity": IDENTITY, "files": files(path)}))
    if terminal:
        generation_hash = digest(path / "manifest.json")
        result = event | {"identity": IDENTITY, "latest_manifest_sha256": generation_hash,
                          "selected_manifest_sha256": generation_hash}
        (root / "direct-result.json").write_text(json.dumps(result))
        (root / "best").write_text(path.name)
    return path


FLAMINGO_IDENTITY = IDENTITY | {"schedule": 14084, "backbone": "flamingo", "binding_sha256": "d" * 64}


def make_flamingo_stop(root, *, bad_promotion=False, bad_recipe=False):
    recipe = FLAMINGO_IDENTITY | {"evaluation_interval": 500, "patience_checks": 4, "min_improvement_ug_m3": "0.02"}
    recipe_pin = hashlib.sha256(json.dumps(
        {k: v for k, v in recipe.items() if k != "logical_run"}, sort_keys=True,
    ).encode()).hexdigest()
    identity = recipe | {"recipe_sha256": "e" * 64 if bad_recipe else recipe_pin,
                         "initial_trainable_sha256": "f" * 64}
    history = []
    for step in range(0, 2501, 500):
        final = step == 2500
        history.append(dict(step=step, best_step=500 if step else None, best_mae=1.0 if step else None,
                            patience=max(0, step // 500 - 1), next_eval_step=None if final else step + 500,
                            outcome="early-stopped" if final else "running",
                            fit_sample={"mae": 1.0},
                            inner_validation={"mae": 0.97 if bad_promotion and step == 1000 else 1.0}))

    manifests = {}
    for step, evaluations in ((500, history[:2]), (2500, history)):
        generation = root / f"step-{step:08d}"
        generation.mkdir()
        (generation / "model").write_bytes(b"synthetic payload")
        (generation / "training.pt.json").write_text(json.dumps({"evaluations": evaluations, "scheduler": {}}))
        manifests[step] = generation / "manifest.json"
        manifests[step].write_text(json.dumps(dict(step=step, identity=identity, files=files(generation))))
    result = history[-1] | dict(identity=identity, latest_manifest_sha256=digest(manifests[2500]),
                                selected_manifest_sha256=digest(manifests[500]))
    (root / "direct-result.json").write_text(json.dumps(result, sort_keys=True))
    (root / "best").write_text("step-00000500\n")
    return result


def retained_selected(path, identity):
    own = dependencies(path, identity)
    history = json.loads((path / "training.pt.json").read_bytes())["evaluations"]
    best = path.parent / f"step-{history[-1]['best_step']:08d}"
    selected = dependencies(best, identity)
    return own | selected if own and selected else set()


FLAMINGO_CHECKPOINT = SimpleNamespace(**(vars(CHECKPOINT) | {"direct_dependencies": retained_selected}))


class FencedReaderTest(unittest.TestCase):
    def test_verified_flamingo_early_stop_only_prices_terminal_recovery_and_retry(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = make_flamingo_stop(root)
            verified, _ = READER["inspect"](root, 1 << 20, FLAMINGO_IDENTITY, FLAMINGO_CHECKPOINT)
            request = dict(run=IDENTITY["logical_run"], seed=27, arm="control", ceiling=14084,
                           pair=dict(fit=IDENTITY["input_manifest_sha256"],
                                     validation=IDENTITY["validation_manifest_sha256"], weights=IDENTITY["weights"],
                                     flamingo_binding=FLAMINGO_IDENTITY["binding_sha256"]))
            completed = dict(attempt_id="paid-1", purpose="selected", request=request,
                             writer_fenced=True, closeout_sha256="a" * 64,
                             flamingo_training_stop=verified["flamingo_training_stop"],
                             flamingo_training_stop_sha256=verified["flamingo_training_stop_sha256"])
            retry_request = request | {"resume": True, "checkpoint": verified["checkpoint"]}
            resumed = dict(attempt_id="paid-2", purpose="selected", request=retry_request,
                           writer_fenced=True, closeout_sha256="b" * 64)
            stopped = dict(attempt_id=resumed["attempt_id"], step=2500, evaluated_through=2500, terminal=True,
                           checkpoint_sha256=result["latest_manifest_sha256"],
                           export_sha256=verified["terminal"]["receipt_sha256"])
            candidate = dict(step=14084, evaluated_through=14084, terminal=True, checkpoint_sha256="c" * 64,
                             export_sha256="d" * 64, **verified_selection("d" * 64, ["2" * 64, "3" * 64]))
            funds = {"fit_attempts": [completed, resumed],
                     "flamingo": {"fit_progress": {"27": {"control": stopped, "candidate": candidate}}}}

            def project():
                return gpu_window.fit_projection(timings("flamingo"), funds, after_preflight=True)

            projection = project()
            for field in ("updates", "evaluations", "replay_updates", "timed_checkpoint_upper_bound"):
                self.assertEqual(projection[field], 0)
            self.assertEqual(projection["job_seconds_by_arm"], {"control": 4})
            self.assertEqual(projection["component_seconds"], {
                "training": 0, "evaluations": 0, "replay": 0, "checkpoints": 0,
                "startup_and_restore": 2, "exports": 1, "selected_restore_probes": 2,
                "terminal_replay_extra": 2, "allocated_preflights": 1,
                "confirmation_5163": 4, "calibration_scoring": 4,
            })
            stopped["attempt_id"] = completed["attempt_id"]
            self.assertEqual(project(), projection)
            stopped["attempt_id"] = resumed["attempt_id"]
            for field, value in (("terminal", False), ("checkpoint_sha256", "7" * 64),
                                 ("export_sha256", "8" * 64), ("evaluated_through", 2000)):
                old = stopped[field]
                stopped[field] = value
                with self.subTest(field=field):
                    self.assertEqual(project()["updates"], 11584)
                stopped[field] = old
            for bad_retry in ({"run": "other-run"}, {"resume": False}, {"checkpoint": "2000:" + "7" * 64}):
                with self.subTest(bad_retry=bad_retry):
                    resumed["request"] = retry_request | bad_retry
                    self.assertEqual(project()["updates"], 11584)
            resumed["request"] = retry_request
            completed["flamingo_training_stop"]["identity"]["recipe_sha256"] = "e" * 64
            self.assertEqual(project()["updates"], 11584)

    def test_flamingo_stop_rejects_wrong_recipe_or_observed_promotion(self):
        for invalid in ("bad_recipe", "bad_promotion"):
            with self.subTest(invalid=invalid), TemporaryDirectory() as directory:
                root = Path(directory)
                make_flamingo_stop(root, **{invalid: True})
                with self.assertRaisesRegex(ValueError, "recipe|promotion"):
                    READER["inspect"](root, 1 << 20, FLAMINGO_IDENTITY, FLAMINGO_CHECKPOINT)

    def test_only_exact_managed_region_label_can_be_injected(self):
        approved = {"app": "airaware-checkpoint-reader", "airaware.training/phase": "checkpoint-reader"}
        region = {"topology.kubernetes.io/region": "eu-south1"}
        verify = POD_LABELS["verify_labels"]
        verify(approved, approved)
        verify(approved, approved | region)
        for observed in (
            approved | {"topology.kubernetes.io/region": "eu-north1"},
            approved | region | {"airaware.training/phase": "fit"},
            approved | region | {"unexpected": "true"},
            {"airaware.training/phase": "checkpoint-reader"} | region,
        ):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                verify(approved, observed)
        with self.assertRaises(ValueError):
            verify(approved | region, approved | region)

    def test_exact_terminal_stream_and_host_digest(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            generation = make_generation(root, 500, terminal=True)
            original = files(root)
            receipt, paths = READER["inspect"](root, 1 << 20, IDENTITY, CHECKPOINT)
            self.assertEqual(receipt["checkpoint"], f"500:{digest(generation / 'manifest.json')}")
            stream = io.BytesIO()
            READER["emit_archive"](stream, paths)
            self.assertEqual(original, files(root))
            output = root / "copy.tar"
            output.write_bytes(stream.getvalue())
            output.chmod(0o600)
            checked = ARCHIVE["verify"](output, receipt, 1 << 20, IDENTITY["logical_run"], 500)
            self.assertEqual(checked["archive_sha256"], digest(output))
            output.write_bytes(stream.getvalue().replace(b"synthetic payload", b"modified_ payload", 1))
            with self.assertRaisesRegex(ValueError, "digest"):
                ARCHIVE["verify"](output, receipt, 1 << 20, IDENTITY["logical_run"], 500)

    def test_mount_content_and_size_guards(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            generation = make_generation(root, 200)
            with self.assertRaisesRegex(ValueError, "bound"):
                READER["inspect"](root, 24, IDENTITY, CHECKPOINT)
            (generation / "extra").symlink_to("/etc/passwd")
            with self.assertRaisesRegex(ValueError, "link"):
                READER["inspect"](root, 1 << 20, IDENTITY, CHECKPOINT)
            (generation / "extra").unlink()
            with self.assertRaisesRegex(ValueError, "different run"):
                READER["inspect"](root, 1 << 20, IDENTITY | {"logical_run": "different"}, CHECKPOINT)

    def test_cumulative_inspection_is_bounded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_generation(root, 100)
            second = make_generation(root, 200)
            bound = max(READER["safe_files"](path, 1 << 20) for path in (first, second))
            with self.assertRaisesRegex(ValueError, "cumulative"):
                READER["inspect"](root, bound, IDENTITY, CHECKPOINT)

    def test_corrupt_latest_preserves_in_bounds_previous_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            earlier = make_generation(root, 100)
            latest = make_generation(root, 200)
            (latest / "model").write_bytes(b"data corruption")
            receipt, paths = READER["inspect"](root, 1 << 20, IDENTITY, CHECKPOINT)
            self.assertEqual(receipt["checkpoint"], f"100:{digest(earlier / 'manifest.json')}")
            self.assertIsNone(receipt["terminal"])
            self.assertTrue(all(name.startswith(earlier.name + "/") for name in paths))


if __name__ == "__main__":
    unittest.main()
