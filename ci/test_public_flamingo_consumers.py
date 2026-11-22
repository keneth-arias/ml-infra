"""The storage observer brackets a complete namespace inventory with the closed guard."""
from pathlib import Path
import runpy
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4


SKY = Path(__file__).resolve().parents[1] / "workloads" / "sky"
sys.path.insert(0, str(SKY))
observer = runpy.run_path(str(SKY / "check-public-flamingo-consumers.py"))
guard = observer["guard"]


class PublicInventoryConsumers(unittest.TestCase):
    def setUp(self):
        self.run = "test"
        self.secret = "stage-test-inventory"
        self.pins = {"policy_uid": str(uuid4()), "binding_uid": str(uuid4()),
                     "policy_generation": 3, "binding_generation": 1}
        self.pods = {"apiVersion": "v1", "kind": "PodList", "metadata": {"resourceVersion": "42"}, "items": []}
        self.calls = []
        self.answers = [True, True]

    def observe(self, verify=None):
        def read_guard(get, run, phase, pins, window=None):
            self.assertEqual((run, phase, pins), (self.run, "closed", self.pins))
            self.calls.append("guard")
            return verify() if verify else self.pins
        def list_pods():
            self.calls.append("pods")
            return self.pods
        def absent(name):
            self.assertEqual(name, self.secret)
            self.calls.append("secret")
            return self.answers.pop(0)
        with patch.object(guard, "verify_guard", side_effect=read_guard):
            return observer["observe"](self.run, self.pins, "storage-observer", None, list_pods, absent)

    def test_complete_inventory_bracketed_by_observations(self):
        receipt = self.observe()
        self.assertEqual(self.calls, ["guard", "secret", "pods", "secret", "guard"])
        self.assertEqual(set(receipt), {"schema", "cluster_id", "run", "storage_operator", "inventory_secret",
                                        "closed_guard", "observed_at", "no_live_consumers", "secret_absent"})
        self.assertEqual(receipt["closed_guard"], self.pins)
        self.assertTrue(receipt["secret_absent"] and receipt["no_live_consumers"])

    def test_successor_scan_binds_both_live_guard_reads_and_receipt(self):
        sha = "7" * 64
        run = "fsw-" + sha[:32]
        window = SimpleNamespace(run=run, sha256=sha)
        pins = dict(self.pins, window_sha256=sha)
        with patch.object(guard, "verify_guard", return_value=pins) as verify:
            result = observer["observe"](run, pins, "reader", None, lambda: self.pods, lambda _: True, window)
        self.assertEqual(verify.call_count, 2)
        for actual in verify.call_args_list:
            self.assertIs(actual.kwargs["window"], window)
        self.assertEqual(result["window_sha256"], sha)
        with self.assertRaises(ValueError):
            observer["observe"](run, self.pins, "reader", None, None, None, window)

    def test_missing_or_partial_inventory_never_attests(self):
        for metadata in ({}, {"resourceVersion": "18", "continue": "partial"}, {"resourceVersion": "18", "remainingItemCount": 1}):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, "complete"):
                self.answers = [True, True]
                self.pods["metadata"] = metadata
                self.observe()

    def test_secret_creation_or_reopened_guard_prevents_issuance(self):
        for answers in ([False], [True, False]):
            with self.subTest(answers=answers), self.assertRaisesRegex(ValueError, "Secret"):
                self.answers = list(answers)
                self.observe()
        self.answers = [True, True]
        results = iter([self.pins, {**self.pins, "policy_generation": 4}])
        with self.assertRaisesRegex(ValueError, "guard"):
            self.observe(lambda: next(results))

    def test_consumers_include_pending_and_deleting_pods(self):
        pod = {"metadata": {"namespace": "training", "deletionTimestamp": "2026-11-22T00:00:00Z"},
               "status": {"phase": "Pending"}, "spec": {"containers": [
                   {"name": "c", "env": [{"name": "x", "valueFrom": {"secretKeyRef": {"name": self.secret, "key": "x"}}}]}]}}
        self.pods["items"] = [pod]
        with self.assertRaisesRegex(ValueError, "live Pod"):
            self.observe()
        self.answers = [True, True]
        pod["status"]["phase"] = "Succeeded"
        self.assertTrue(self.observe()["no_live_consumers"])


if __name__ == "__main__":
    unittest.main()
