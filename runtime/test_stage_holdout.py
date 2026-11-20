import json
import tempfile
import unittest
from pathlib import Path

from episode_release import retain, sha
from stage_holdout import stage


class HoldoutStageTest(unittest.TestCase):
    def test_stages_only_the_sealed_infer_bundle_and_rejects_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort = root / "cohort"
            keep = root / "model-state"
            inputs = root / "inputs"
            cp = root / "step-00000002"
            cp.mkdir()
            (cp / "weights").write_bytes(b"checkpoint")

            def put(path, obj):
                path.write_text(json.dumps(obj, sort_keys=True))
                return sha(path)

            ck = put(
                cp / "manifest.json",
                {
                    "step": 2,
                    "files": {"weights": {"size": 10, "sha256": sha(cp / "weights")}},
                },
            )
            image = "ghcr.io/test/repo@sha256:" + "c" * 64
            base = "b" * 64
            ps = "a" * 64
            seal = put(
                root / "policy.json",
                {
                    "version": "lima-concentracion-prueba-congelada-v1",
                    "scope": "retrospective_concentration_only",
                    "head": "direct",
                    "seed": 27,
                    "protocol_sha256": ps,
                    "base_weights_sha256": base,
                    "checkpoint_sha256": ck,
                    "image": image,
                },
            )
            components = {
                n: put(root / n, {}) for n in ("calibration.json", "transform.json")
            }
            components["policy.json"] = seal
            rel = put(
                root / "release.json",
                {
                    "version": 1,
                    "mode": "concentration",
                    "purpose": "retrospective_holdout_only",
                    "image": image,
                    "base_manifest_sha256": base,
                    "checkpoint_sha256": ck,
                    "components": components,
                },
            )
            retain(root / "release.json", rel, cp, keep / "episodes/retained")
            infer = cohort / "infer"
            infer.mkdir(parents=True)
            (cohort / "scoring").mkdir()
            (cohort / "scoring/observed.npz").write_bytes(b"held-out")
            data = infer / "data.npz"
            data.write_bytes(b"history")
            bs = put(
                infer / "manifest.json",
                {
                    "schema": "forecast-input-v1",
                    "phase": "infer",
                    "head": "direct",
                    "block": "external_test",
                    "unit": "ug/m3",
                    "weights_manifest_sha256": base,
                    "checkpoint_sha256": ck,
                    "seal_sha256": seal,
                    "files": {"data.npz": sha(data)},
                },
            )
            rec = {
                "version": "lima-cohorte-campo-marte-v1",
                "protocol_sha256": ps,
                "seal_sha256": seal,
                "files": {"infer/manifest.json": bs},
                "eligible_histories": 0,
            }

            def run():
                rs = put(cohort / "receipt.json", rec)
                return stage(cohort, rs, inputs, keep, "station-test", rel, 2)

            self.assertEqual(run()["status"], "empty")
            self.assertFalse(inputs.exists())
            rec["eligible_histories"] = 1
            self.assertEqual(run(), run())
            target = inputs / "airaware/episodes/station-test"
            self.assertEqual(
                {p.name for p in (target / "infer").iterdir()},
                {"data.npz", "manifest.json"},
            )
            self.assertFalse((target / "scoring").exists())
            self.assertFalse((keep / "episodes/station-test/state").exists())
            self.assertTrue((keep / "episodes/station-test/output").is_dir())
            (target / "infer/data.npz").write_bytes(b"other")
            with self.assertRaisesRegex(ValueError, "Corrupt"):
                run()
            rec["seal_sha256"] = "d" * 64
            with self.assertRaisesRegex(ValueError, "sealed"):
                run()
