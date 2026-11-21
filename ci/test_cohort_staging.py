"""Cover versioned and local staging for the two separately pinned training roles."""

import hashlib
import io
import json
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
        cmd = job["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertEqual(cmd[cmd.index("--validation-sha256") + 1], "b" * 64)
        args[args.index("--phase") + 1] = "infer"
        self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
