from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bootstrap/nebius/management/native-activation.py"
RECEIPT = "a" * 64


class NativeActivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = runpy.run_path(str(TOOL))
        cls.policy, cls.binding = cls.functions["held_policy_and_binding"]()
        cls.images = {
            role: next(v["expression"][1:-1] for v in cls.policy["spec"]["variables"]
                       if v["name"] == role + "Image")
            for role in ("trainer", "preflight")
        }

    def test_held_default_and_only_bounded_policy_changes(self):
        make = self.functions["active_policy"]
        hold = self.policy["spec"]["validations"]
        self.assertEqual(hold[0]["expression"], "false")
        support = make(self.images, RECEIPT, "support")
        gpu = make(self.images, RECEIPT, "gpu")
        self.assertEqual(gpu["spec"]["validations"], hold[1:])
        self.assertEqual(support["spec"]["validations"][1:], hold[1:])
        self.assertIn("request.dryRun", support["spec"]["validations"][0]["expression"])
        self.assertEqual(support["spec"]["variables"], self.policy["spec"]["variables"])
        with self.assertRaises(ValueError):
            make(self.images | {"trainer": self.images["preflight"]}, RECEIPT, "gpu")

    def test_live_policy_binding_generation_and_warnings_are_gated(self):
        make = self.functions["active_policy"]
        check = self.functions["check_active"]
        expected = make(self.images, RECEIPT, "support")
        live = deepcopy(expected)
        live["metadata"]["generation"] = 7
        live["status"] = {"observedGeneration": 7, "typeChecking": {}}
        check(expected, self.binding, {"items": [live, self.binding]})
        for edit in (
            lambda p, b: p["spec"]["validations"].pop(),
            lambda p, b: p["metadata"]["annotations"].clear(),
            lambda p, b: p["status"].update(observedGeneration=6),
            lambda p, b: p["status"]["typeChecking"].update(expressionWarnings=[{"warning": "bad"}]),
            lambda p, b: b["spec"].update(validationActions=["Warn"]),
        ):
            p, b = deepcopy(live), deepcopy(self.binding)
            edit(p, b)
            with self.assertRaises(ValueError):
                check(expected, self.binding, {"items": [p, b]})

    def test_access_exact_scope(self):
        role, binding = self.functions["access_objects"](RECEIPT)["items"]
        self.assertEqual(role["metadata"]["namespace"], "training")
        self.assertEqual(role["rules"], [
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        ])
        self.assertEqual(binding["subjects"], [
            {"kind": "ServiceAccount", "name": "episode-submit", "namespace": "training"}
        ])

    def test_window_scope_rejects_wrong_target_and_elapsed_cutoff(self):
        check = self.functions["require_window"]
        scope = {"stage": "support", "cluster_id": "mk8scluster-fixture",
                 "start": "2026-01-01T00:00:00Z", "cutoff": "2026-01-01T01:00:00Z"}
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "scope.json"
            path.write_text(json.dumps(scope))
            path.chmod(0o600)
            check(path, "support", scope["cluster_id"], now)
            for stage, cluster, observed in (
                ("gpu", scope["cluster_id"], now),
                ("support", "mk8scluster-other", now),
                ("support", scope["cluster_id"], now.replace(minute=50)),
            ):
                with self.assertRaises(ValueError):
                    check(path, stage, cluster, observed)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                check(path, "support", scope["cluster_id"], now)
