import sys
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
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[1] / "bootstrap/nebius")
        )
        from worker_cleanup import release_candidates

        self.check = release_candidates
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

    def test_refuse_owned_noop_and_wrong_cutoff_delete(self):
        change = self.plan["resource_changes"][0]["change"]
        change["actions"] = ["no-op"]
        with self.assertRaisesRegex(ValueError, "Unexpected change"):
            self.check_release()
        change["actions"] = ["delete"]
        self.before["labels"]["airaware-cutoff"] = "20261120090000"
        with self.assertRaisesRegex(ValueError, "Unexpected change"):
            self.check_release()

    def test_wrong_cluster_or_concurrent_changes(self):
        self.before["parent_id"] = "another-cluster"
        with self.assertRaises(ValueError):
            self.check_release()
        self.before["parent_id"] = "cluster-1"
        self.plan["resource_changes"].extend(
            plan("unexpected", ["update"])["resource_changes"]
        )
        with self.assertRaises(ValueError):
            self.check_release()


class LiveCleanupTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[1] / "bootstrap/nebius")
        )
        import worker_cleanup

        self.cleanup = worker_cleanup
        self.baseline = dict(
            project_id="project",
            cluster_id="cluster",
            group_ids=[],
            disk_ids=[],
            instance_ids=[],
        )
        self.support = dict(
            stage="gpu",
            support_group_id="cpu-group",
            support_instance_id="cpu-vm",
            support_boot_disk_id="cpu-boot",
        )
        self.live = {
            "node-group": {"cpu-group": {}},
            "instance": {"cpu-vm": {}},
            "disk": {"cpu-boot": {}},
        }

    def verify(self, stage):
        from unittest.mock import patch

        with patch.object(
            self.cleanup,
            "cloud_items",
            side_effect=lambda _, svc, resource, parent: self.live[resource],
        ):
            self.cleanup.verify_inventory(
                "ml-prod", "project", "cluster", self.baseline, before_stage=stage
            )

    def test_exact_support_ids_and_orphan_denial(self):
        self.verify(self.support)
        with self.assertRaises(ValueError):
            self.verify({"stage": "pilot"})
        for resource in self.live:
            with self.subTest(resource=resource):
                self.live[resource]["orphan"] = {}
                with self.assertRaises(ValueError):
                    self.verify(self.support)
                del self.live[resource]["orphan"]
        del self.live["instance"]["cpu-vm"]
        with self.assertRaises(ValueError):
            self.verify(self.support)

    def test_targeted_delete_must_match_live_ids_labels_and_addresses(self):
        from copy import deepcopy

        address = 'nebius_mk8s_v1_node_group.training["cpu"]'
        doc = plan(address, ["delete"])
        doc.update(complete=False, applyable=True)
        before = {
            "id": "cpu-group",
            "parent_id": "cluster",
            "labels": {"airaware-run": "fit", "airaware-cutoff": "cut"},
        }
        doc["resource_changes"][0]["change"]["before"] = before
        expected = {"target_ids": {"cpu-group"}}
        self.assertEqual(
            self.cleanup.release_candidates(doc, "cluster", "fit", "cut", **expected),
            [address],
        )
        # During actual expiry the managed CPU is releasable even if a GPU never entered state.
        self.assertEqual(
            self.cleanup.release_candidates(
                doc,
                "cluster",
                "fit",
                "cut",
                target_ids={"cpu-group", "orphan-gpu"},
                allow_orphans=True,
            ),
            [address],
        )
        for case in ("foreign-id", "orphan", "wrong-run", "extra-resource", "deferred"):
            modified = deepcopy(doc)
            opts = dict(expected)
            if case == "foreign-id":
                modified["resource_changes"][0]["change"]["before"]["id"] = "unapproved"
            elif case == "orphan":
                opts["target_ids"] = {"cpu-group", "another-live"}
            elif case == "wrong-run":
                modified["resource_changes"][0]["change"]["before"]["labels"][
                    "airaware-run"
                ] = "other"
            elif case == "extra-resource":
                modified["resource_changes"].extend(
                    plan("nebius_storage_v1_bucket.artifacts[0]", ["delete"])[
                        "resource_changes"
                    ]
                )
            else:
                modified["deferred_changes"] = [{}]
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.cleanup.release_candidates(
                    modified, "cluster", "fit", "cut", **opts
                )

    def test_expiry_releases_managed_subset_and_exits_nonzero_for_orphan(self):
        from unittest.mock import Mock, patch

        main = runpy.run_path(
            str(Path(self.cleanup.__file__).with_name("expire-workers"))
        )["main"]
        apply = Mock()
        with (
            patch.dict(
                main.__globals__,
                plan_release=Mock(return_value=({"cpu"}, {"gpu-orphan"})),
                terraform=apply,
            ),
            patch.object(
                sys,
                "argv",
                [
                    "expire-workers",
                    "--cluster-id",
                    "cluster",
                    "--run-id",
                    "fit",
                    "--cutoff",
                    "2020-01-01T00:00:00Z",
                    "--apply",
                ],
            ),
            self.assertRaisesRegex(SystemExit, "reconcile"),
        ):
            main()
        self.assertEqual(apply.call_count, 1)
        self.assertEqual(apply.call_args.args[0], "apply")

    def test_different_terraform_credential_cannot_use_cli_grant(self):
        from tempfile import TemporaryDirectory

        script = (
            Path(self.cleanup.__file__).parent / "management/publish-timer-attestation"
        )
        verify = runpy.run_path(str(script))["provider_key"]
        with TemporaryDirectory() as tmp:
            key = Path(tmp) / "fixture"
            key.touch()
            other = key.with_name("other")
            other.touch()
            cfg = {
                "service-account-id": "acct",
                "public-key-id": "key",
                "private-key-file-path": str(key),
            }
            env = {
                "NEBIUS_SA_ID": "acct",
                "NEBIUS_AUTHORIZED_KEY_ID": "key",
                "NEBIUS_PRIVATE_KEY_FILE": str(key),
            }
            self.assertEqual(verify("acct", "acct", cfg.__getitem__, env), "key")
            for field, value in (
                ("NEBIUS_SA_ID", "other"),
                ("NEBIUS_AUTHORIZED_KEY_ID", "other"),
                ("NEBIUS_PRIVATE_KEY_FILE", str(other)),
            ):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    verify("acct", "acct", cfg.__getitem__, dict(env, **{field: value}))
