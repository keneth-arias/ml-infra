"""Release must preserve all original retained volumes, detached and healthy."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import yaml


STORAGE = Path(__file__).resolve().parents[1] / "platform/retained-storage"
sys.path.insert(0, str(STORAGE))
from closeout import load_pinned, verify_closeout
from check import CLAIMS, OWNER, reconcile


class RetainedCloseoutTest(unittest.TestCase):
    def setUp(self):
        self.approval = dict(project_id="project-p", cluster_id="mk8scluster-c", reservation_id="storage-r",
                             approved_at="2026-10-01T10:00:00Z", acquisition_deadline="2026-10-01T11:00:00Z")
        sc = yaml.safe_load((STORAGE / "db/storage-class.yaml").read_bytes())
        self.live = dict(project_id="project-p", cluster_id="mk8scluster-c",
                         namespace_uids={"training": "t", "tracking": "m"}, storage_classes=[sc],
                         claims=[], volumes=[], disks=[], attachments=[])
        for (ns, name), size in CLAIMS.items():
            i = len(self.live["claims"])
            pv, uid, disk = f"pv-{i}", f"uid-{i}", f"computedisk-{i}"
            meta = dict(name=name, namespace=ns, uid=uid, labels={OWNER: "storage-r"},
                        creationTimestamp="2026-10-01T10:02:00Z")
            spec = dict(storageClassName=sc["metadata"]["name"], accessModes=["ReadWriteOnce"])
            self.live["claims"].append(dict(metadata=meta, status=dict(phase="Bound"),
                                            spec=spec | dict(volumeName=pv, resources=dict(requests=dict(storage=f"{size}Gi")))))
            self.live["volumes"].append(dict(metadata=dict(name=pv, uid=f"pv-uid-{i}", creationTimestamp="2026-10-01T10:02:05Z"),
                                             status=dict(phase="Bound"), spec=spec | dict(
                                                 persistentVolumeReclaimPolicy="Retain", capacity=dict(storage=f"{size}Gi"),
                                                 csi=dict(driver=sc["provisioner"], fsType="ext4", volumeHandle=disk),
                                                 claimRef=dict(name=name, namespace=ns, uid=uid))))
            self.live["disks"].append(dict(metadata=dict(id=disk, parent_id="project-p", created_at="2026-10-01T10:02:02Z"),
                                           status=dict(state="READY"), spec=dict(type="NETWORK_SSD", size_gibibytes=size)))
        claims, volumes, disks = (self.live[k] for k in ("claims", "volumes", "disks"))
        db = reconcile("db", self.approval, sc, claims[:1], volumes[:1], disks[:1], [])
        self.original = reconcile("all", self.approval, sc, claims, volumes, disks, [], db)
        self.original["namespace_uids"] = self.live["namespace_uids"]
        self.baseline = self.original["baseline"]

    def test_exact_retention_after_acquisition_window(self):
        proof = verify_closeout(self.approval, self.original, self.baseline, self.live)
        self.assertEqual(proof["phase"], "worker-release")
        self.assertEqual(proof["baseline"], self.baseline)
        self.assertEqual(proof["bindings"], self.original["bindings"])

    def test_refuses_drift_in_retained_identity_and_attachment(self):
        def replace_pvc(live):
            live["claims"][1]["metadata"]["uid"] = "recreated"
            live["volumes"][1]["spec"]["claimRef"]["uid"] = "recreated"
        for mutation in (
            lambda live: live["disks"].pop(), lambda live: live["attachments"].append({"uid": "va"}),
            lambda live: live["namespace_uids"].update(training="new-namespace"),
            lambda live: live["disks"][2]["status"].update(read_write_attachment="instance-1"),
            lambda live: live["disks"][1]["status"].update(managed_by="instance-1"),
            lambda live: live["volumes"][1]["metadata"].update(uid="recreated-pv"),
            lambda live: live["disks"][0]["metadata"].update(created_at="2026-10-01T10:03:00Z"),
            replace_pvc,
        ):
            live = deepcopy(self.live)
            mutation(live)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                verify_closeout(self.approval, self.original, self.baseline, live)

    def test_mismatched_baseline_and_unpinned_file_bytes(self):
        with self.assertRaises(ValueError):
            verify_closeout(self.approval, self.original, self.baseline | {"disk_ids": []}, self.live)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.json"
            path.write_bytes(json.dumps(self.baseline).encode())
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(load_pinned(path, digest), self.baseline)
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                load_pinned(path, digest)
