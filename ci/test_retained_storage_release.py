"""Exercise operator-only retained storage renders and funded binding checks."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
import unittest

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
        self.assertEqual(storage_class["parameters"], {"type": "NETWORK_SSD", "csi.storage.k8s.io/fstype": "ext4"})
        for objects, expected in ((database, {("tracking", "postgres", "16Gi")}),
                                   (training, {("training", "airaware-checkpoints", "64Gi"),
                                               ("training", "airaware-model-state", "64Gi")})):
            claims = [o for o in objects if o["kind"] == "PersistentVolumeClaim"]
            self.assertEqual({(o["metadata"]["namespace"], o["metadata"]["name"],
                               o["spec"]["resources"]["requests"]["storage"]) for o in claims}, expected)
            self.assertTrue(all(o["spec"]["storageClassName"] == storage_class["metadata"]["name"]
                                and o["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
                                for o in claims))

    def test_bounds_and_exact_binding(self):
        spec = importlib.util.spec_from_file_location("volumes", STORAGE / "check.py")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        approval = dict(reservation_id="synthetic-1", project_id="project-synthetic", cluster_id="mk8scluster-synthetic",
                        approved_at="2026-10-01T20:00:00Z", acquisition_deadline="2026-10-01T21:00:00Z",
                        retain_until="2026-10-31T21:00:00Z", allocated_non_gpu_usd="20",
                        ssd_gib_hourly_usd="0.0001", tax_reserve_rate="0.22", retention_owner="test")
        now = datetime(2026, 10, 1, 20, 20, tzinfo=timezone.utc)
        self.assertGreater(helper.funding(approval, now), 10)
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
        self.assertEqual(all_claims["baseline"]["disk_ids"], [item["metadata"]["id"] for item in disks])
        self.assertNotIn("baseline", first)
        with self.assertRaises(ValueError):
            helper.reconcile("all", approval, sc, claims, volumes, disks, [])
        volumes[0]["spec"]["claimRef"]["uid"] = "wrong"
        with self.assertRaises(ValueError):
            helper.reconcile("all", approval, sc, claims, volumes, disks, [], first)


if __name__ == "__main__":
    unittest.main()
