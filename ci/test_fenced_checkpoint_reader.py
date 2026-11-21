"""Test the attended checkpoint reader and host archive verifier with synthetic bytes."""

import hashlib
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest


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


class FencedReaderTest(unittest.TestCase):
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
