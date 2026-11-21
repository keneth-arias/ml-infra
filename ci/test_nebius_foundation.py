import runpy
import unittest
from pathlib import Path


check_plan = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "bootstrap/nebius/apply-foundation")
)["check_plan"]


def plan(address, actions):
    return {
        "format_version": "1.2",
        "complete": True,
        "errored": False,
        "resource_changes": [
            {"address": address, "mode": "managed", "change": {"actions": actions}}
        ],
    }


class FoundationTest(unittest.TestCase):
    def test_allow_foundation_creates(self):
        for address in (
            "nebius_mk8s_v1_cluster.training[0]",
            "nebius_billing_v1_pricing_policy.training[0]",
        ):
            check_plan(plan(address, ["create"]))

    def test_deny_workers_unknowns_and_replacements(self):
        for address, actions in (
            ('nebius_mk8s_v1_node_group.training["cpu"]', ["create"]),
            ('nebius_mk8s_v1_node_group.training["gpu"]', ["no-op"]),
            ("nebius_compute_v1_instance.unexpected", ["create"]),
            ("nebius_mk8s_v1_cluster.training[0]", ["delete", "create"]),
        ):
            with self.subTest(address=address), self.assertRaises(ValueError):
                check_plan(plan(address, actions))

    def test_require_complete_plan(self):
        with self.assertRaises(ValueError):
            check_plan({"complete": False, "resource_changes": []})


class ExpiryTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "bootstrap/nebius/expire-workers"
        self.check = runpy.run_path(str(path))["release_candidates"]
        self.plan = plan('nebius_mk8s_v1_node_group.training["gpu"]', ["delete"])
        self.before = {
            "parent_id": "cluster-1",
            "labels": {"airaware-run": "run-1", "airaware-cutoff": "20261120080000"},
        }
        self.plan["resource_changes"][0]["change"]["before"] = self.before

    def check_release(self):
        return self.check(self.plan, "cluster-1", "run-1", "20261120080000")

    def test_only_exact_owned_deletion(self):
        self.assertEqual(len(self.check_release()), 1)
        self.before["labels"]["airaware-run"] = "another-run"
        with self.assertRaises(ValueError):
            self.check_release()

    def test_wrong_cluster_or_concurrent_changes(self):
        self.before["parent_id"] = "another-cluster"
        with self.assertRaises(ValueError):
            self.check_release()
        self.before["parent_id"] = "cluster-1"
        self.plan["resource_changes"].extend(plan("unexpected", ["update"])["resource_changes"])
        with self.assertRaises(ValueError):
            self.check_release()
