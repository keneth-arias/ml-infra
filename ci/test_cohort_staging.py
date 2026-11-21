"""Cover versioned and local staging for the two separately pinned training roles."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from versioned_episode_inputs import ObjectSet, sources


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CohortStageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / "base"
        weights = self.base / "weights" / "tiny"
        weights.parent.mkdir(parents=True)
        weights.write_bytes(b"fixed")
        (self.base / "SHA256SUMS").write_text(f"{sha(weights)}  weights/tiny\n")
        self.weights = sha(self.base / "SHA256SUMS")

    def role(self, name, protocol="b" * 64):
        folder = self.root / name
        folder.mkdir()
        (folder / "data.npz").write_bytes(name.encode())
        (folder / "thresholds.json").write_text(
            json.dumps(
                {"fit_ids_sha256": "a" * 64, "unit": "ug/m3", "stations": {"s": 1.0}}
            )
        )
        spec = {
            "schema": "forecast-direct-cohort-v1",
            "unit": "ug/m3",
            "head": "direct",
            "phase": name,
            "weights_manifest_sha256": self.weights,
            "windows": 2,
            "ids_sha256": "a" * 64 if name == "fit" else "d" * 64,
            "protocol_sha256": protocol,
            "source_manifest_sha256": "e" * 64,
            "files": {p.name: sha(p) for p in folder.iterdir()},
        }
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps(spec))
        return sha(manifest)

    def launch(self, fit, validation, path):
        return subprocess.run(
            [
                sys.executable,
                ROOT / "runtime/stage_episode_inputs.py",
                "--base",
                self.base,
                "--bundle",
                self.root / "fit",
                "--validation-bundle",
                self.root / path,
                "--cache",
                self.root / "cache",
                "--phase",
                "fit",
                "--head",
                "direct",
                "--weights-sha256",
                self.weights,
                "--bundle-sha256",
                fit,
                "--validation-sha256",
                validation,
            ],
            text=True,
            capture_output=True,
        )

    def test_cold_warm_and_reject_swapped_role(self):
        fit, validation = self.role("fit"), self.role("inner_validation")
        for cache in ("miss", "hit"):
            result = self.launch(fit, validation, "inner_validation")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {v["cache"] for v in json.loads(result.stdout).values()}, {cache}
            )
        scorer = self.role("confirmation")
        denied = self.launch(fit, scorer, "confirmation")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("another phase", denied.stderr)
        self.assertFalse((self.root / "cache/bundles" / scorer).exists())

    def test_incompatible_protocol_not_published(self):
        fit, validation = self.role("fit"), self.role("inner_validation", "f" * 64)
        denied = self.launch(fit, validation, "inner_validation")
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn("cohort protocol", denied.stderr)
        self.assertFalse((self.root / "cache/bundles" / validation).exists())

    def test_concurrent_publishers_and_corrupt_cache_hit(self):
        fit, validation = self.role("fit"), self.role("inner_validation")
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = [pool.submit(self.launch, fit, validation, "inner_validation") for _ in range(4)]
            receipts = []
            for job in jobs:
                result = job.result()
                self.assertEqual(result.returncode, 0, result.stderr)
                receipts.append(json.loads(result.stdout))
        for role in ("phase", "inner_validation", "weights"):
            self.assertCountEqual([r[role]["cache"] for r in receipts], ["miss", "hit", "hit", "hit"])
        data = self.root / "cache/bundles" / fit / "data.npz"
        data.write_bytes(b"corrupted")
        denied = self.launch(fit, validation, "inner_validation")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(data.read_bytes(), b"corrupted")

    def test_retry_recovers_only_its_interrupted_copy(self):
        fit, validation = self.role("fit"), self.role("inner_validation")
        source = self.root / "fit"
        source.chmod(0o555)
        self.addCleanup(source.chmod, 0o700)
        bundles = self.root / "cache/bundles"
        worker = """
import os, signal, sys
from pathlib import Path
from stage_episode_inputs import publish
parent, key, source = sys.argv[1:]
def interrupt(stage):
    stage.chmod(0o555)
    os.kill(os.getpid(), signal.SIGKILL)
publish(Path(parent), key, Path(source), interrupt)
"""
        for key in (fit, "c" * 64):
            result = subprocess.run(
                [sys.executable, "-c", worker, bundles, key, self.root / "fit"],
                cwd=ROOT / "runtime",
            )
            self.assertEqual(result.returncode, -signal.SIGKILL)
        partial = bundles / (".staging-" + fit)
        other = bundles / (".staging-" + "c" * 64)
        self.assertTrue(partial.is_dir())
        result = self.launch(fit, validation, "inner_validation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["phase"]["cache"], "miss")
        self.assertFalse(partial.exists())
        self.assertTrue(other.is_dir())
        self.assertEqual(source.stat().st_mode & 0o777, 0o555)
        hit = self.launch(fit, validation, "inner_validation")
        self.assertEqual(hit.returncode, 0, hit.stderr)
        self.assertEqual(json.loads(hit.stdout)["phase"]["cache"], "hit")

    def test_inventory_restricts_each_role_to_its_own_prefix(self):
        prefix = "https://storage.eu-south1.nebius.cloud/example/run/"

        def entry(name, role):
            return {
                "name": name,
                "url": prefix + role + "/" + name + "?versionId=1",
                "version_id": "1",
                "size": 1,
            }

        inv = {
            "base": [entry("SHA256SUMS", "base")],
            "bundle": [entry("manifest.json", "trainer/fit")],
            "inner_validation": [entry("manifest.json", "trainer/inner_validation")],
        }
        path = self.root / "inventory.json"
        path.write_text(json.dumps(inv))
        self.assertEqual(len(sources(path, prefix, paired=True)), 3)
        inv["inner_validation"] = [entry("manifest.json", "scorer/confirmation")]
        path.write_text(json.dumps(inv))
        with self.assertRaises(ValueError):
            sources(path, prefix, paired=True)

    def test_unpaired_inventory_requires_base_and_bundle_lists(self):
        prefix = "https://storage.eu-south1.nebius.cloud/example/run/"
        path = self.root / "inventory.json"
        for invalid in ([], {"base": []}, {"base": [], "bundle": {}},
                        {"base": [], "bundle": [], "inner_validation": []}):
            with self.subTest(invalid=invalid):
                path.write_text(json.dumps(invalid))
                with self.assertRaisesRegex(ValueError, "inventory"):
                    sources(path, prefix)

    def test_invalid_remote_manifest_prevents_payload_download(self):
        prefix = "https://storage.eu-south1.nebius.cloud/example/run/trainer/fit/"
        rows = [
            {
                "name": name,
                "url": prefix + name + "?versionId=1",
                "version_id": "1",
                "size": 2,
            }
            for name in ("data.npz", "manifest.json")
        ]
        manifest = io.BytesIO(b"{}")
        manifest.headers = {"x-amz-version-id": "1", "Content-Length": "2"}

        def reject(_):
            raise ValueError("Role mismatch")

        with patch("versioned_episode_inputs.build_opener") as opener:
            opener.return_value.open.return_value = manifest
            with self.assertRaisesRegex(ValueError, "Role mismatch"):
                ObjectSet(rows, prefix, False, exact_keys=True).copy(
                    self.root / "out", reject
                )
            opener.return_value.open.assert_called_once_with(rows[1]["url"], timeout=30)
        self.assertFalse((self.root / "out/data.npz").exists())

    def test_versioned_download_enforces_metadata_and_exact_size(self):
        prefix = "https://storage.eu-south1.nebius.cloud/example/run/trainer/fit/"
        record = {
            "name": "data.npz",
            "url": prefix + "data.npz?versionId=1&signature=private-fixture",
            "version_id": "1",
            "size": 2,
        }
        files = ObjectSet([record], prefix, False, exact_keys=True)
        cases = [
            ("valid", "1", "2", b"ab"),
            ("version", "2", "2", b"ab"),
            ("length", "1", "3", b"ab"),
            ("short", "1", "2", b"a"),
            ("oversize", "1", "2", b"abc"),
        ]
        for name, version, length, payload in cases:
            with self.subTest(name=name), patch("versioned_episode_inputs.build_opener") as opener:
                response = io.BytesIO(payload)
                response.headers = {"x-amz-version-id": version, "Content-Length": length}
                opener.return_value.open.return_value = response
                target = self.root / name
                if name == "valid":
                    files.copy(target)
                    self.assertEqual((target / "data.npz").read_bytes(), b"ab")
                else:
                    with self.assertRaisesRegex(OSError, "Versioned download failed"):
                        files.copy(target)
        with patch("versioned_episode_inputs.build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError(record["url"])
            with self.assertRaises(OSError) as caught:
                files.copy(self.root / "network-error")
            self.assertIn("TimeoutError", str(caught.exception))
            self.assertNotIn("private-fixture", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_render_paired_staging(self):
        args = [
            sys.executable,
            ROOT / "workloads/sky/stage-episode.py",
            "--image",
            "image@sha256:" + "0" * 64,
            "--inventory-secret",
            "training-sources",
            "--source-prefix",
            "https://storage.eu-south1.nebius.cloud/example/run/",
            "--weights-sha256",
            self.weights,
            "--bundle-sha256",
            "a" * 64,
            "--phase",
            "fit",
            "--head",
            "direct",
            "--validation-sha256",
            "b" * 64,
        ]
        job = json.loads(subprocess.check_output(args))
        self.assertEqual(job["spec"]["ttlSecondsAfterFinished"], 86400)
        pod = job["spec"]["template"]["spec"]
        inputs = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "inputs")
        self.assertEqual(inputs, {"name": "inputs", "mountPath": "/inputs", "subPath": "airaware/cache"})
        self.assertEqual(pod["securityContext"]["fsGroup"], 1000)
        cmd = job["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertEqual(cmd[cmd.index("--validation-sha256") + 1], "b" * 64)
        args[args.index("--phase") + 1] = "infer"
        self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)

    def test_legacy_point_control_files_remain_loadable(self):
        prefix = "https://storage.eu-south1.nebius.cloud/example/run/"
        rows = [
            {
                "name": name,
                "url": prefix + name + "?versionId=1",
                "version_id": "1",
                "size": 4,
            }
            for name in (
                "data.npz",
                "manifest.json",
                "quantile-fit.json",
                "quantile-checkpoint.json",
            )
        ]
        self.assertEqual(
            set(ObjectSet(rows, prefix, False).records), {r["name"] for r in rows}
        )
        with self.assertRaises(ValueError):
            ObjectSet(rows, prefix, False, exact_keys=True)
