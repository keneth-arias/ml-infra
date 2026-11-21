"""Verify that moving the RWO training volume fails closed on live users."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


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
        receipt = {"cluster_id": "managed", "phase": "all", "bindings": [{
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
            self.assertEqual(handoff.approved_binding(path, digest, "managed"), observed)
            with self.assertRaisesRegex(ValueError, "approved digest"):
                handoff.approved_binding(path, "0" * 64, "managed")
            with self.assertRaisesRegex(ValueError, "this deployment"):
                handoff.approved_binding(path, digest, "other")

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
