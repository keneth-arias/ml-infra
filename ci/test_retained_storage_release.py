"""Exercise operator-only retained storage renders and funded binding checks."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "platform/retained-storage"


def render(path):
    result = subprocess.check_output(["kubectl", "kustomize", path])
    return list(yaml.safe_load_all(result))


class RetainedStorageReleaseTest(unittest.TestCase):
    def test_default_roots_do_not_declare_claims(self):
        for path in ("clusters/prod-nebius", "platform/overlays/prod", "bootstrap/nebius/management/tracking"):
            with self.subTest(path=path):
                self.assertFalse(any(obj["kind"] == "PersistentVolumeClaim" for obj in render(ROOT / path)))

    def test_opt_in_db_and_training_are_disjoint(self):
        database = render(STORAGE / "db")
        training = render(STORAGE / "training")
        self.assertEqual({o["kind"] for o in database}, {"StorageClass", "PersistentVolumeClaim"})
        storage_class, = [o for o in database if o["kind"] == "StorageClass"]
        self.assertEqual(storage_class["provisioner"], "compute.csi.nebius.com")
        self.assertEqual(storage_class["volumeBindingMode"], "Immediate")
        self.assertEqual(storage_class["reclaimPolicy"], "Retain")
        self.assertEqual(storage_class["allowedTopologies"], [
            {"matchLabelExpressions": [{"key": "kubernetes.io/os", "values": ["linux"]}]},
        ])
        self.assertEqual(storage_class["parameters"], {"type": "NETWORK_SSD", "csi.storage.k8s.io/fstype": "ext4"})
        for objects, expected in ((database, {("tracking", "postgres", "16Gi")}),
                                   (training, {("training", "airaware-checkpoints", "128Gi")})):
            claims = [o for o in objects if o["kind"] == "PersistentVolumeClaim"]
            self.assertEqual({(o["metadata"]["namespace"], o["metadata"]["name"],
                               o["spec"]["resources"]["requests"]["storage"]) for o in claims}, expected)
            self.assertTrue(all(o["spec"]["storageClassName"] == storage_class["metadata"]["name"]
                                and o["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
                                for o in claims))

    def test_quota_counts_two_retained_disks_and_two_worker_boots(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        gib = 1024**3
        cases = [(4, 400, 0, 0, True), (3, 400, 0, 0, False), (4, 399, 0, 0, False),
                 (4, 400, 1, 16, True), (4, 400, 2, 144, True)]
        for count, size, owned, owned_gib, allowed in cases:
            for extra_count, extra_gib in ((0, 0), (1, 0), (0, 1)):
                with self.subTest(count=count, capacity=size, owned=owned, extra_count=extra_count, extra_gib=extra_gib):
                    count_quota = {"spec": {"limit": count}, "status": {"usage": owned + extra_count}}
                    size_quota = {"spec": {"limit": size * gib},
                                  "status": {"usage": (owned_gib + extra_gib) * gib, "unit": "byte"}}
                    with patch.object(helper, "query", side_effect=(count_quota, size_quota)) as query:
                        if allowed and not (extra_count or extra_gib):
                            evidence = helper.disk_quota([], "tenant-example", owned, owned_gib)
                            self.assertIn("compute.disk.count", query.call_args_list[0].args[0])
                            self.assertIn("compute.disk.size.network-ssd", query.call_args_list[1].args[0])
                            self.assertEqual(evidence, {"disk_quota_limit": count, "disk_quota_bytes_limit": size * gib})
                        else:
                            with self.assertRaises(ValueError):
                                helper.disk_quota([], "tenant-example", owned, owned_gib)

        quota = {"spec": {"limit": 400 * gib}, "status": {"unit": "byte", "usage_state": "USAGE_STATE_NOT_USED"}}
        with patch.object(helper, "query", return_value=quota):
            self.assertEqual(helper.quota_headroom([], "tenant-example", "disk-size", 400 * gib, 0, unit="byte"),
                             400 * gib)
            with self.assertRaisesRegex(ValueError, "below the verified"):
                helper.quota_headroom([], "tenant-example", "disk-size", 400 * gib, 16 * gib, unit="byte")
            quota["status"]["usage"] = 8 * gib
            with self.assertRaisesRegex(ValueError, "below the verified"):
                helper.quota_headroom([], "tenant-example", "disk-size", 400 * gib, 16 * gib, unit="byte")
            del quota["status"]["usage"]
            quota["status"]["usage_state"] = "UNKNOWN"
            with self.assertRaisesRegex(ValueError, "did not report usage"):
                helper.quota_headroom([], "tenant-example", "disk-size", 400 * gib, 0, unit="byte")
            quota["status"]["unit"] = "gib"
            with self.assertRaisesRegex(ValueError, "Unexpected unit"):
                helper.quota_headroom([], "tenant-example", "disk-size", 400 * gib, 0, unit="byte")

    def test_submission_fails_closed_before_create(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        now = datetime(2026, 10, 1, 20, 20, tzinfo=timezone.utc)
        approval = {"acquisition_deadline": "2026-10-01T20:20:30Z"}
        self.assertLessEqual(helper.submission_seconds(approval, now, now), 30)
        with self.assertRaises(ValueError):
            helper.submission_seconds(approval, now - timedelta(minutes=2), now)
        with self.assertRaises(ValueError):
            helper.submission_seconds(approval, now, now + timedelta(seconds=30))
        with patch.object(helper, "capture", side_effect=ValueError("quota")), patch.object(helper, "create_object") as create:
            with self.assertRaisesRegex(ValueError, "quota"):
                helper.submit(type("Args", (), {"phase": "submit-db"})(), approval, "a" * 64)
            create.assert_not_called()

    def test_funding_read_cannot_outlive_the_last_create_deadline(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        start = datetime(2026, 10, 1, 20, 20, tzinfo=timezone.utc)
        deadline = start + timedelta(seconds=30)
        approval = {
            "tenant_id": "tenant-test", "cluster_id": "mk8scluster-test",
            "reservation_id": "test", "operator_username": "operator",
            "acquisition_deadline": deadline.isoformat(),
        }
        args = type("Args", (), {"phase": "submit-db", "profile": "unused", "endpoint": "public",
                                "ca_sha256": "a" * 64})()
        with (patch.object(helper, "datetime", wraps=datetime) as clock,
              patch.object(helper, "capture", return_value={}),
              patch.object(helper, "kube", return_value=["kubectl"]),
              patch.object(helper, "pinned_kubeconfig_ca"),
              patch.object(helper, "query", return_value={"status": {"userInfo": {"username": "operator"}}}),
              patch.object(helper, "disk_quota"), patch.object(helper, "funding") as funding,
              patch.object(helper, "create_object") as create):
            clock.now.return_value = start
            def slow_funding(*_):
                clock.now.return_value = deadline

            funding.side_effect = slow_funding
            with self.assertRaisesRegex(ValueError, "expired"):
                helper.submit(args, approval, "a" * 64)
            create.assert_not_called()

    def test_creation_uses_strict_create_only_and_pins_response_uid(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        args = SimpleNamespace(kubeconfig=Path("/example/kubeconfig"), context="example")
        claim = {"kind": "PersistentVolumeClaim", "metadata": {"name": "postgres", "namespace": "tracking",
                 "labels": {helper.OWNER: "approved"}}}
        result = {**claim, "metadata": {**claim["metadata"], "uid": "created-uid"}}
        with patch.object(helper.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(result))) as run:
            receipt = helper.create_object(args, claim, 7)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[-6:], ["create", "--validate=strict", "-f", "-", "-o", "json"])
        self.assertIn("--request-timeout=7s", cmd)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(receipt["uid"], "created-uid")
        identity = {("PersistentVolumeClaim", "tracking", "postgres")}
        helper.submitted_uids({"created_objects": [receipt]}, [result], identity)
        result["metadata"]["uid"] = "different"
        with self.assertRaises(ValueError):
            helper.submitted_uids({"created_objects": [receipt]}, [result], identity)

    def test_future_or_old_phase_evidence_is_rejected(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        live = dict(observed_at="2026-10-01T20:20:00Z", approval_sha256="a" * 64,
                    namespace_uids={"tracking": "tracking-uid", "training": "training-uid"},
                    project_id="project-synthetic", cluster_id="mk8scluster-synthetic")
        funding = {"approved_at": "2026-10-01T20:00:00Z"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            for observed_at in ("2026-10-01T19:59:59Z", "2026-10-01T20:20:01Z"):
                path.write_text(json.dumps(live | dict(phase="db", observed_at=observed_at)))
                with self.assertRaises(ValueError):
                    helper.evidence(path, "db", live, funding)
            path.write_text(json.dumps(live | {"phase": "submit-db"}))
            with self.assertRaises(ValueError):
                helper.evidence(path, "db", live, funding)

    def test_repaired_capture_preserves_original_paid_claim_and_exact_receipts(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        state = dict(project_id="project-synthetic", cluster_id="mk8scluster-synthetic",
                     namespace_uids={"tracking": "tracking-uid", "training": "training-uid"})
        approval = state | dict(reservation_id="synthetic", tenant_id="tenant-synthetic",
                                approved_at="2026-10-01T20:00:00Z", acquisition_deadline="2026-10-01T21:00:00Z",
                                retain_until="2026-10-31T21:00:00Z", allocated_non_gpu_usd="20",
                                ssd_gib_hourly_usd="0.0001", tax_reserve_rate="0.22", retention_owner="test")
        approval_hash = "a" * 64
        repair = dict(project_id=state["project_id"], cluster_id=state["cluster_id"],
                      namespace_uid="tracking-uid", approval_sha256=approval_hash, reservation_id="synthetic",
                      pvc_uid="db-uid", storage_class_uid="class-uid", attempt_id="repair-1",
                      started_at="2026-10-01T20:20:00.500000Z")
        cls, claim = sorted(render(STORAGE / "db"), key=lambda row: row["kind"], reverse=True)
        cls["metadata"].update(uid="class-uid", creationTimestamp="2026-10-01T20:01:00Z",
                               labels={helper.OWNER: "synthetic"},
                               annotations={helper.REPAIR_ATTEMPT: repair["attempt_id"],
                                            helper.REPAIR_STARTED: repair["started_at"]})
        claim["metadata"].update(uid="db-uid", creationTimestamp="2026-10-01T20:02:00Z",
                                 labels={helper.OWNER: "synthetic"})
        claim["spec"]["volumeName"] = "db-pv"
        claim["status"] = {"phase": "Bound"}
        provider_id = "computedisk-synthetic"
        pv = dict(metadata=dict(name="db-pv", uid="pv-uid", creationTimestamp="2026-10-01T20:20:01Z"),
                  status={"phase": "Bound"},
                  spec=dict(storageClassName=helper.CLASS, persistentVolumeReclaimPolicy="Retain",
                            accessModes=["ReadWriteOnce"], capacity={"storage": "16Gi"},
                            claimRef={"namespace": "tracking", "name": "postgres", "uid": "db-uid"},
                            csi=dict(driver=helper.DRIVER, fsType="ext4", volumeHandle=provider_id)))
        disk = dict(metadata=dict(id=provider_id, parent_id=state["project_id"],
                                  created_at="2026-10-01T20:20:01.300000Z"),
                    spec=dict(type="NETWORK_SSD", size_bytes="17179869184"),
                    status={"state": "READY", "size_bytes": "17179869184"})
        live = state | dict(storage_classes=[cls], claims=[claim], volumes=[pv], disks=[disk], attachments=[])
        created = [{"key": helper.object_key(obj), "uid": obj["metadata"]["uid"]} for obj in (cls, claim)]
        with TemporaryDirectory() as directory:
            submission = Path(directory) / "submitted.json"
            submission.write_text(json.dumps(state | dict(phase="submit-db", approval_sha256=approval_hash,
                                  observed_at="2026-10-01T20:02:01Z", created_objects=created)))
            args = SimpleNamespace(phase="db", profile="ml-prod", submission=submission, db_repair=repair)
            with (patch.object(helper, "observe", return_value=live), patch.object(helper, "disk_quota", return_value={}) as disk_quota,
                  patch.object(helper, "datetime", wraps=datetime) as clock):
                clock.now.return_value = datetime.fromisoformat("2026-10-01T20:24:30Z")
                result = helper.capture(args, approval, approval_hash)
                disk_quota.assert_called_once_with(unittest.mock.ANY, approval["tenant_id"], 1, 16)
                self.assertEqual(result["db_repair"], repair)
                self.assertEqual(result["bindings"][0]["pvc_uid"], "db-uid")
                for invalid in (None, repair | {"pvc_uid": "foreign"}, repair | {"extra": "foreign"},
                                repair | {"attempt_id": "different"}):
                    args.db_repair = invalid
                    with self.assertRaisesRegex(ValueError, "topology repair"):
                        helper.capture(args, approval, approval_hash)
                args.db_repair = repair
                clock.now.return_value = datetime.fromisoformat("2026-10-01T20:26:00Z")
                with self.assertRaisesRegex(ValueError, "five-minute"):
                    helper.capture(args, approval, approval_hash)
                clock.now.return_value = datetime.fromisoformat("2026-10-01T20:24:30Z")
                disk["metadata"]["created_at"] = "2026-10-01T20:20:00.100000Z"
                with self.assertRaisesRegex(ValueError, "after the topology patch"):
                    helper.capture(args, approval, approval_hash)
                disk["metadata"]["created_at"] = result["bindings"][0]["disk_created_at"]
                args.phase = "submit-training"
                args.db_receipt = Path(directory) / "db-bound.json"
                raw = json.dumps(result).encode()
                args.db_receipt.write_bytes(raw)
                args.db_receipt_sha256 = hashlib.sha256(raw).hexdigest()
                self.assertEqual(helper.capture(args, approval, approval_hash)["db_repair"], repair)
                args.db_receipt.write_bytes(raw + b" ")
                with self.assertRaisesRegex(ValueError, "receipt digest"):
                    helper.capture(args, approval, approval_hash)

                args.db_receipt.write_bytes(raw)
                training, = render(STORAGE / "training")
                training["metadata"].update(uid="training-pvc-uid", creationTimestamp="2026-10-01T20:28:00Z",
                                            labels={helper.OWNER: "synthetic"})
                training["spec"]["volumeName"] = "training-pv"
                training["status"] = {"phase": "Bound"}
                training_pv = deepcopy(pv)
                training_pv["metadata"].update(name="training-pv", uid="training-pv-uid",
                                               creationTimestamp="2026-10-01T20:28:01Z")
                training_pv["spec"]["capacity"] = {"storage": "128Gi"}
                training_pv["spec"]["claimRef"] = {
                    key: training["metadata"][key] for key in ("namespace", "name", "uid")
                }
                training_disk = deepcopy(disk)
                training_disk["metadata"].update(id="computedisk-training", created_at="2026-10-01T20:28:01Z")
                training_disk["spec"]["size_bytes"] = "137438953472"
                training_disk["status"]["size_bytes"] = "137438953472"
                training_pv["spec"]["csi"]["volumeHandle"] = training_disk["metadata"]["id"]
                live["claims"].append(training)
                live["volumes"].append(training_pv)
                live["disks"].append(training_disk)
                args.phase = "all"
                args.submission = Path(directory) / "training-submission.json"
                submission = state | dict(phase="submit-training", approval_sha256=approval_hash,
                                          observed_at="2026-10-01T20:28:01Z", db_repair=repair,
                                          created_objects=[{"key": helper.object_key(training),
                                                            "uid": training["metadata"]["uid"]}])
                args.submission.write_text(json.dumps(submission))
                clock.now.return_value = datetime.fromisoformat("2026-10-01T20:29:00Z")
                final = helper.capture(args, approval, approval_hash)
                self.assertEqual(final["db_repair"], repair)
                self.assertEqual(final["baseline"]["disk_ids"], [provider_id, "computedisk-training"])
                args.submission.write_text(json.dumps(submission | {"db_repair": None}))
                with self.assertRaisesRegex(ValueError, "Training submission differs"):
                    helper.capture(args, approval, approval_hash)

    def test_bounds_and_exact_binding(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        approval = dict(reservation_id="synthetic-1", project_id="project-synthetic", cluster_id="mk8scluster-synthetic",
                        approved_at="2026-10-01T20:00:00Z", acquisition_deadline="2026-10-01T21:00:00Z",
                        retain_until="2026-10-31T21:00:00Z", allocated_non_gpu_usd="20",
                        ssd_gib_hourly_usd="0.0001", tax_reserve_rate="0.22", retention_owner="test")
        now = datetime(2026, 10, 1, 20, 20, tzinfo=timezone.utc)
        self.assertEqual(helper.funding(approval, now), Decimal("12.666528"))
        with self.assertRaises(ValueError):
            helper.funding(approval | {"allocated_non_gpu_usd": "9"}, now)
        sc, = [o for o in render(STORAGE / "db") if o["kind"] == "StorageClass"]
        claims = [o for root in ("db", "training") for o in render(STORAGE / root) if o["kind"] == "PersistentVolumeClaim"]
        volumes, disks = [], []
        for i, claim in enumerate(claims):
            meta, claim_spec = claim["metadata"], claim["spec"]
            meta.update(uid=f"claim-{i}", labels={helper.OWNER: "synthetic-1"})
            name = f"volume-{i}"
            disk_id = f"computedisk-synthetic{i}"
            claim_spec["volumeName"] = name
            claim["status"] = {"phase": "Bound"}
            size = claim_spec["resources"]["requests"]["storage"]
            volumes.append(dict(metadata=dict(name=name, uid=f"volume-uid-{i}"), status={"phase": "Bound"},
                                spec=dict(storageClassName=helper.CLASS, persistentVolumeReclaimPolicy="Retain",
                                          accessModes=["ReadWriteOnce"], capacity={"storage": size},
                                          csi=dict(driver=helper.DRIVER, fsType="ext4", volumeHandle=disk_id),
                                          claimRef={k: meta[k] for k in ("name", "namespace", "uid")})))
            disks.append(dict(metadata=dict(id=disk_id, parent_id=approval["project_id"]),
                              spec=dict(type="NETWORK_SSD", size_gibibytes=size.removesuffix("Gi")), status={"state": "READY"}))
        first = helper.reconcile("db", approval, sc, claims[:1], volumes[:1], disks[:1], [])
        all_claims = helper.reconcile("all", approval, sc, claims, volumes, disks, [], first)
        self.assertEqual(len(all_claims["bindings"]), 2)
        self.assertEqual(all_claims["baseline"]["disk_ids"], [item["metadata"]["id"] for item in disks])
        self.assertNotIn("baseline", first)
        with self.assertRaises(ValueError):
            helper.reconcile("all", approval, sc, claims, volumes, disks, [])
        for obj in (claims[0], volumes[0]):
            obj["metadata"]["deletionTimestamp"] = "2026-10-01T20:20:00Z"
            with self.assertRaises(ValueError):
                helper.reconcile("all", approval, sc, claims, volumes, disks, [], first)
            obj["metadata"].pop("deletionTimestamp")
        disks[0]["status"]["managed_by"] = "computeinstance-synthetic"
        with self.assertRaises(ValueError):
            helper.reconcile("all", approval, sc, claims, volumes, disks, [], first)
        del disks[0]["status"]["managed_by"]
        volumes[0]["spec"]["claimRef"]["uid"] = "wrong"
        with self.assertRaises(ValueError):
            helper.reconcile("all", approval, sc, claims, volumes, disks, [], first)


if __name__ == "__main__":
    unittest.main()
