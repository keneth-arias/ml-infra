"""CPU-only tests of the managed baseline review, collection order and handoff."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

SMOKE = Path(__file__).resolve().parents[1] / "workloads/smoke"
RENDER = runpy.run_path(str(SMOKE / "render-flamingo-baseline.py"))
collect = runpy.run_path(str(SMOKE / "flamingo-baseline-worker.py"))["collect"]
promote = runpy.run_path(str(SMOKE / "promote-flamingo-baselines.py"))["promote"]
PIN = "a" * 64
IMAGE = "ghcr.io/keneth-arias/ml-infra-qwen@sha256:" + PIN


class BaselineTest(unittest.TestCase):
    def test_bounded_render_and_scoped_mounts(self):
        render = RENDER["render"]
        spec = render(IMAGE, "base-a", "gpu-1", 600, 50_000_000_000)["spec"]
        self.assertEqual([spec[k] for k in ("parallelism", "completions", "backoffLimit", "activeDeadlineSeconds")],
                         [1, 1, 0, 600])
        pod = spec["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["nodeSelector"], {"airaware.training/pool": "training-gpu"})
        fields = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
        self.assertEqual(fields[0]["matchFields"][0]["values"], ["gpu-1"])
        c, = pod["containers"]
        self.assertEqual(c["command"][3:], [(SMOKE / "flamingo-baseline-worker.py").read_text(), "sha256:" + PIN])
        for role in ("requests", "limits"):
            self.assertEqual(c["resources"][role]["nvidia.com/gpu"], "1")
        self.assertEqual(c["volumeMounts"][:2], [
            {"name": "inputs", "mountPath": "/base", "readOnly": True,
             "subPath": "airaware/cache/weights/" + RENDER["FLAMINGO_WEIGHTS"]},
            {"name": "inputs", "mountPath": "/output", "subPath": "flamingo-baselines/base-a"}])
        for args in ((IMAGE, "../run", "gpu-1", 300, 1), ("latest", "abc", "node", 1, 1),
                     (IMAGE, "abc", "node", 3601, 1), (IMAGE, "abc", "node", 1, 96_000_000_001)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                render(*args)

    def test_review_verifies_target_and_never_creates(self):
        main = RENDER["main"]
        node = {"metadata": {"name": "gpu-1", "labels": {"airaware.training/pool": "training-gpu"}},
                "spec": {"providerID": "vm-1"}, "status": {"allocatable": {"nvidia.com/gpu": "1"},
                "conditions": [{"type": "Ready", "status": "True"}]}}
        args = ["render", "--node", "gpu-1", "--provider-id", "vm-1", "--expected-deployment", PIN,
                "--run", "example", "--deadline-seconds", "300", "--gpu-limit-bytes", "100"]
        with (patch.dict(os.environ, NEBIUS_STAGING_OPERATOR_IDENTITY="reader", NEBIUS_TRAINING_CLUSTER_ID="target"),
              patch.object(sys, "argv", args),
              patch.dict(main.__globals__, current_release=lambda: ({"trainer": IMAGE}, PIN)),
              patch.dict(main.__globals__, kubectl_client_for_identity=self.client),
              patch("subprocess.check_output", return_value=json.dumps(node)) as call,
              contextlib.redirect_stdout(io.StringIO()) as out):
            main()
            call.assert_called_once_with(["verified", "get", "node", "gpu-1", "-o", "json"], timeout=15)
            review = json.loads(out.getvalue())
            self.assertEqual(review["status"], "pending-admission-and-funding")
            self.assertEqual(review["job_sha256"], hashlib.sha256(review["job_json"].encode()).hexdigest())
            node["spec"]["providerID"] = "another-vm"
            call.return_value = json.dumps(node)
            with self.assertRaisesRegex(ValueError, "allocation"):
                main()
            with patch.dict(main.__globals__, current_release=lambda: ({"trainer": IMAGE}, "b" * 64)):
                with self.assertRaisesRegex(ValueError, "deployment"):
                    main()

    def client(self, namespace, identity, *, direct):
        self.assertEqual((namespace, identity, direct), ("training", "reader", True))
        return ["verified"]

    def test_two_collectors_freeze_and_atomic_promotion(self):
        raw = [b'{"process":"one"}', b'{"process":"two"}']
        pins = [hashlib.sha256(b).hexdigest() for b in raw]
        paths = []

        def base(argv, *, cwd, check):
            self.assertTrue(check)
            self.assertEqual(cwd, "/opt/forecast/src")
            self.assertEqual(argv[1:-1], ["-m", "opentslm.flamingo_qualification", "base", "--weights",
                                         "/base/weights", "--image-digest", "sha256:" + PIN, "--output"])
            Path(argv[-1]).write_bytes(raw[len(paths)])
            paths.append(argv[-1])

        def freeze(argv, *, cwd):
            self.assertEqual(argv[1:], ["-m", "opentslm.flamingo_qualification", "freeze", "--baselines", *paths])
            self.assertEqual(len(paths), 2)
            return json.dumps({"baseline_receipts_sha256": pins}).encode()

        with (TemporaryDirectory() as tmp, patch("subprocess.run", side_effect=base),
              patch("subprocess.check_output", side_effect=freeze) as comparison, contextlib.redirect_stdout(io.StringIO())):
            root = Path(tmp)
            source = root / "baseline"
            state = root / "state"
            state.mkdir()
            comparison.side_effect = ValueError("freeze failed")
            with self.assertRaises(ValueError):
                collect("sha256:" + PIN, root)
            self.assertFalse(source.exists())
            paths.clear()
            comparison.side_effect = freeze
            collect("sha256:" + PIN, root)
            with self.assertRaises(FileExistsError):
                collect("sha256:" + PIN, root)
            with self.assertRaisesRegex(ValueError, "approval"):
                promote(source, state, pins[::-1])
            p = source / (pins[1] + ".json")
            p.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "approval"):
                promote(source, state, pins)
            self.assertEqual(list(state.iterdir()), [])
            p.write_bytes(raw[1])
            promote(source, state, pins)
            promote(source, state, pins)
            for expected, pin in zip(raw, pins):
                self.assertEqual((state / "flamingo-baselines" / (pin + ".json")).read_bytes(), expected)
            (state / "flamingo-baselines" / (pins[0] + ".json")).write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError, "approval"):
                promote(source, state, pins)


if __name__ == "__main__":
    unittest.main()
