import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

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
                    "inference_batch": 2,
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
            np.savez(
                data,
                window_id=np.asarray([], dtype=str),
                history=np.empty((0, 24), dtype="f4"),
            )
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
            del rec["eligible_histories"]
            with self.assertRaisesRegex(ValueError, "eligible_histories"):
                run()
            for invalid in (False, "0", 169, -1):
                rec["eligible_histories"] = invalid
                with self.assertRaisesRegex(ValueError, "eligible_histories"):
                    run()
            rec["eligible_histories"] = 1
            with self.assertRaisesRegex(ValueError, "rows disagree"):
                run()
            spec = json.loads((infer / "manifest.json").read_bytes())

            def history_of(value):
                np.savez(data, window_id=np.array(["w1"]), history=np.full((1, 24), value))
                spec["files"]["data.npz"] = sha(data)
                digest = put(infer / "manifest.json", spec)
                rec["files"]["infer/manifest.json"] = digest
                return digest

            for malformed in (1e39, -1e39, 2j, "1.0"):
                with self.subTest(value=malformed):
                    history_of(malformed)
                    with self.assertRaisesRegex(ValueError, "FP32"):
                        run()
                    self.assertFalse(inputs.exists())
            bs = history_of(np.float32(0))
            plan = run()
            self.assertEqual(plan, run())
            launch = root / "launch.json"
            put(launch, plan)
            script = Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py"
            rendered = subprocess.check_output([
                sys.executable, str(script), "preflight", "infer", "direct", "station-test",
                "--image", image, "--bundle-sha256", bs, "--step", "2", "--batch", "2",
                "--retained-release", rel, "--staged-launch", str(launch),
            ])
            pod = json.loads(rendered)["spec"]["template"]["spec"]
            worker = pod["containers"][0]
            mounts = {m["mountPath"]: m for m in worker["volumeMounts"]}
            checkpoint = "/checkpoint/step-00000002"
            self.assertEqual(set(mounts), {"/bundle", "/base", checkpoint})
            self.assertEqual(mounts[checkpoint]["subPath"], f"episodes/retained/{rel}/step-00000002")
            self.assertTrue(all(m["readOnly"] for m in mounts.values()))
            self.assertEqual(worker["resources"], {"requests": {"cpu": "2", "memory": "1G"}})
            self.assertNotIn("runtimeClassName", pod)
            self.assertIn("--preflight", worker["command"])

            renderer = runpy.run_path(str(script))
            validate = renderer["validate_staged_launch"]
            valid = [launch, rel, bs, "station-test", "direct", image, 27, 2, 2]
            validate(*valid)
            for i, wrong in (
                (1, "0" * 64),
                (2, "1" * 64),
                (3, "different"),
                (4, "quantile"),
                (5, image.replace("ghcr.io", "docker.io")),
                (6, 28),
                (7, 3),
                (8, 16),
            ):
                with self.subTest(i=i), self.assertRaises(ValueError):
                    args = valid.copy()
                    args[i] = wrong
                    validate(*args)
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
