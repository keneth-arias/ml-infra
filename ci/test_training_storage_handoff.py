"""Verify that moving the RWO training volume fails closed on live users."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SKY = Path(__file__).resolve().parents[1] / "workloads/sky"
sys.path.insert(0, str(SKY))
module = importlib.util.spec_from_file_location("training_storage", SKY / "training-storage-handoff.py")
handoff = importlib.util.module_from_spec(module)
module.loader.exec_module(handoff)


def fixture():
    return {
        "pvc": {"metadata": {"uid": "pvc-uid"}, "status": {"phase": "Bound"},
                "spec": {"volumeName": "pv", "storageClassName": "airaware-network-retain-immediate",
                         "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "128Gi"}}}},
        "pv": {"metadata": {"uid": "pv-uid"}, "status": {"phase": "Bound"},
               "spec": {"storageClassName": "airaware-network-retain-immediate", "capacity": {"storage": "128Gi"},
                        "accessModes": ["ReadWriteOnce"], "persistentVolumeReclaimPolicy": "Retain",
                        "claimRef": {"namespace": "training", "name": "airaware-checkpoints", "uid": "pvc-uid"},
                        "csi": {"driver": "compute.csi.nebius.com", "fsType": "ext4", "volumeHandle": "computedisk-provider"}}},
        "pods": {"items": []},
        "volumeattachments": {"items": []},
    }


class TrainingStorageTest(unittest.TestCase):
    def test_terminal_consumer_allowed_only_after_attachment_disappears(self):
        data = fixture()
        active = {"status": {"phase": "Running"}, "spec": {"volumes": [
            {"persistentVolumeClaim": {"claimName": "airaware-checkpoints"}}]}}
        data["pods"]["items"] = [active]
        self.assertFalse(handoff.status(lambda resource, *_: data[resource])[1])
        active["status"]["phase"] = "Succeeded"
        data["volumeattachments"]["items"] = [{"spec": {"source": {"persistentVolumeName": "pv"}}, "status": {"attached": False}}]
        self.assertFalse(handoff.status(lambda resource, *_: data[resource])[1])
        data["volumeattachments"]["items"].clear()
        self.assertTrue(handoff.status(lambda resource, *_: data[resource])[1])

    def test_approved_binding_matches_exact_observed_disk(self):
        receipt = {"cluster_id": "managed", "project_id": "project-training", "phase": "all", "bindings": [{
            "namespace": "training", "claim": "airaware-checkpoints", "size_gib": 128,
            "pvc_uid": "pvc-uid", "pv_uid": "pv-uid",
            "csi_handle": "computedisk-provider", "provider_disk_id": "computedisk-provider",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "all-bound.json"
            raw = json.dumps(receipt).encode()
            path.write_bytes(raw)
            path.chmod(0o600)
            digest = hashlib.sha256(raw).hexdigest()
            data = fixture()
            observed, _ = handoff.status(lambda resource, *_: data[resource])
            self.assertEqual(handoff.approved_binding(path, digest, "managed"), ("project-training", observed))
            with self.assertRaisesRegex(ValueError, "approved digest"):
                handoff.approved_binding(path, "0" * 64, "managed")
            with self.assertRaisesRegex(ValueError, "this deployment"):
                handoff.approved_binding(path, digest, "other")

    def test_provider_must_report_exact_ready_detached_disk(self):
        disk = {
            "metadata": {"id": "computedisk-provider", "parent_id": "project-training"},
            "spec": {"type": "NETWORK_SSD", "size_gibibytes": "128"},
            "status": {"state": "READY"},
        }
        def detached():
            return handoff.provider_detached(disk, "project-training", "computedisk-provider")

        self.assertTrue(detached())
        for key, value in (
            ("state", "CREATING"), ("reconciling", True), ("managed_by", "computeinstance-owner"),
            ("read_write_attachment", {"instance_id": "computeinstance-writer"}),
            ("read_only_attachments", [{"instance_id": "computeinstance-reader"}]),
        ):
            with self.subTest(key=key):
                disk["status"][key] = value
                self.assertFalse(detached())
                disk["status"] = {"state": "READY", "reconciling": False}
                self.assertTrue(detached())
        for scope, key, value in (
            ("metadata", "parent_id", "project-other"), ("metadata", "id", "computedisk-other"),
            ("spec", "type", "NETWORK_SSD_IO_M3"), ("spec", "size_gibibytes", 127),
        ):
            with self.subTest(key=key):
                original = disk[scope][key]
                disk[scope][key] = value
                with self.assertRaisesRegex(ValueError, "funded training disk"):
                    detached()
                disk[scope][key] = original

    def test_provider_size_units_are_exact_and_consistent(self):
        for field, value in (
            ("size_bytes", "137438953472"), ("size_kibibytes", 134217728),
            ("size_mebibytes", "131072"), ("size_gibibytes", 128), ("size_gibibytes", "128"),
        ):
            with self.subTest(field=field, value=value):
                self.assertEqual(handoff.disk_size_gib({
                    "spec": {field: value}, "status": {"size_bytes": "137438953472"},
                }), 128)
        for spec in (
            {}, {"size_gibibytes": 128, "size_bytes": 129 << 30},
            {"size_bytes": (128 << 30) + 1}, {"size_mebibytes": 129},
            {"size_gibibytes": True}, {"size_gibibytes": -1}, {"size_gibibytes": 0},
            {"size_gibibytes": 128.0}, {"size_gibibytes": "128.0"},
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                handoff.disk_size_gib({"spec": spec})
        with self.assertRaisesRegex(ValueError, "differs"):
            handoff.disk_size_gib({"spec": {"size_gibibytes": 128}, "status": {"size_bytes": "17179869184"}})

    def test_handoff_polls_provider_until_detached(self):
        binding = ("pvc-uid", "pv-uid", "computedisk-provider")
        disk = {
            "metadata": {"id": binding[2], "parent_id": "project-training"},
            "spec": {"type": "NETWORK_SSD", "size_bytes": "137438953472"},
            "status": {"state": "READY", "size_bytes": "137438953472"},
        }
        attached = {**disk, "status": {"state": "READY", "reconciling": True}}
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "kubeconfig"
            profile.touch(mode=0o600)
            env = {"NEBIUS_STORAGE_KUBECONFIG": str(profile), "NEBIUS_STORAGE_OPERATOR_IDENTITY": "storage",
                   "NEBIUS_TRAINING_CLUSTER_ID": "managed", "NEBIUS_CA_BUNDLE_SHA256": "test",
                   "NEBIUS_STORAGE_BINDING_RECEIPT": "all-bound.json", "NEBIUS_STORAGE_BINDING_SHA256": "test"}
            with (
                patch.dict(os.environ, env),
                patch.object(handoff, "pinned_kubeconfig_ca"),
                patch.object(handoff, "approved_binding", return_value=("project-training", binding)),
                patch.object(handoff, "status", side_effect=[(binding, False), (binding, True), (binding, True)]),
                patch.object(handoff, "sleep") as sleep,
                patch.object(handoff.subprocess, "check_output",
                             side_effect=["storage", json.dumps(attached), json.dumps(disk)]) as cli,
            ):
                handoff.main()
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(cli.call_count, 3)
        for call in cli.call_args_list[1:]:
            command = call.args[0]
            self.assertEqual(command[:3], ["nebius", "-p", "ml-prod"])
            self.assertEqual(command[-4:], ["compute", "disk", "get", binding[2]])
            self.assertLessEqual(call.kwargs["timeout"], 10)

    def test_legacy_or_incomplete_binding_denied(self):
        for path in ("pvc", "pv"):
            data = fixture()
            data[path]["metadata"]["deletionTimestamp"] = "2026-11-22T00:00:00Z"
            with self.assertRaises(ValueError):
                handoff.status(lambda resource, *_: data[resource])
        data = fixture()
        data["volumeattachments"]["metadata"] = {"continue": "more"}
        with self.assertRaises(ValueError):
            handoff.status(lambda resource, *_: data[resource])


if __name__ == "__main__":
    unittest.main()
