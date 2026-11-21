#!/usr/bin/env python3
"""Read managed storage evidence and build an exact worker-release inventory."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "bootstrap/nebius"), str(ROOT / "workloads/sky")]
from private_api import pinned_kubeconfig_ca
from worker_cleanup import cloud_items
from worker_plan import amount


CLASS = "airaware-network-retain-immediate"
DRIVER = "compute.csi.nebius.com"
OWNER = "airaware.storage/reservation"
CLAIMS = {("tracking", "postgres"): 16, ("training", "airaware-checkpoints"): 64,
          ("training", "airaware-model-state"): 64}
DB = ("tracking", "postgres")


def utc(value):
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() != timedelta(0):
        raise ValueError("Expected UTC approval timestamps")
    return moment


def funding(approval, now):
    approved, deadline, expiry = (utc(approval[key]) for key in
                                  ("approved_at", "acquisition_deadline", "retain_until"))
    if not approved <= now < deadline < expiry:
        raise ValueError("Outside the approved storage acquisition window")
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", approval["reservation_id"]):
        raise ValueError("Expected a valid storage reservation label")
    if not approval["retention_owner"].strip():
        raise ValueError("Expected a named retention owner")
    rate, tax, ceiling = (amount(approval[key]) for key in
                          ("ssd_gib_hourly_usd", "tax_reserve_rate", "allocated_non_gpu_usd"))
    hours = Decimal((expiry - approved) // timedelta(microseconds=1)) / 3_600_000_000
    bound = sum(CLAIMS.values()) * hours * rate * (1 + tax)
    if not 0 < bound <= ceiling <= 100:
        raise ValueError("The funded storage envelope does not cover retention")
    return bound


def indexed(rows, identity):
    result = {identity(row): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Duplicate identity in storage evidence")
    return result


def reconcile(phase, approval, storage_class, claims, volumes, disks, attachments, db_receipt=None):
    if storage_class["metadata"]["name"] != CLASS or (
        storage_class["provisioner"], storage_class["volumeBindingMode"], storage_class["reclaimPolicy"],
        storage_class["parameters"],
    ) != (DRIVER, "Immediate", "Retain", {"type": "NETWORK_SSD", "csi.storage.k8s.io/fstype": "ext4"}):
        raise ValueError("The retained StorageClass differs from the reviewed manifest")
    expected = {DB: CLAIMS[DB]} if phase == "db" else CLAIMS
    claims = indexed(claims, lambda row: (row["metadata"]["namespace"], row["metadata"]["name"]))
    volumes = indexed(volumes, lambda row: row["metadata"]["name"])
    disks = indexed(disks, lambda row: row["metadata"]["id"])
    if attachments or claims.keys() != expected.keys() or not len(volumes) == len(disks) == len(expected):
        raise ValueError("Unexpected claims, PVs, disks or attachments in the storage-only stage")
    reservation = approval["reservation_id"]
    project, cluster = (approval[key] for key in ("project_id", "cluster_id"))
    bindings = []
    handles = set()
    for (namespace, name), size in expected.items():
        claim = claims[namespace, name]
        meta, spec = claim["metadata"], claim["spec"]
        pv_name = spec.get("volumeName")
        if (claim.get("status", {}).get("phase") != "Bound" or pv_name not in volumes
                or meta.get("labels", {}).get(OWNER) != reservation or not meta.get("uid")
                or spec["storageClassName"] != CLASS or spec["accessModes"] != ["ReadWriteOnce"]
                or spec.get("volumeMode", "Filesystem") != "Filesystem"
                or spec["resources"]["requests"]["storage"] != f"{size}Gi"):
            raise ValueError("An approved final claim is not Bound to its reviewed storage class")
        pv = volumes[pv_name]
        volume = pv["spec"]
        ref, csi = volume.get("claimRef", {}), volume.get("csi", {})
        handle = csi.get("volumeHandle", "")
        if (not pv["metadata"].get("uid") or pv.get("status", {}).get("phase") != "Bound"
                or (ref.get("namespace"), ref.get("name"), ref.get("uid")) != (namespace, name, meta["uid"])
                or volume["persistentVolumeReclaimPolicy"] != "Retain" or volume["storageClassName"] != CLASS
                or volume["capacity"]["storage"] != f"{size}Gi" or volume["accessModes"] != ["ReadWriteOnce"]
                or volume.get("volumeMode", "Filesystem") != "Filesystem"
                or csi.get("driver") != DRIVER or csi.get("fsType") != "ext4"
                or not re.fullmatch(r"computedisk-[a-z0-9]+", handle) or handle not in disks or handle in handles):
            raise ValueError("PVC-to-PV ownership or the exact CSI disk identity differs")
        disk = disks[handle]
        status = disk["status"]
        if (disk["metadata"]["parent_id"] != project or disk["spec"]["type"] != "NETWORK_SSD"
                or int(disk["spec"]["size_gibibytes"]) != size or status["state"] != "READY"
                or status.get("reconciling") or status.get("read_write_attachment")
                or status.get("read_only_attachments")):
            raise ValueError("Expected the exact Ready and detached workload-project Network SSD")
        handles.add(handle)
        bindings.append(dict(namespace=namespace, claim=name, pvc_uid=meta["uid"], pv=pv_name,
                             pv_uid=pv["metadata"]["uid"], csi_handle=handle, provider_disk_id=handle,
                             disk_created_at=disk["metadata"].get("created_at"), size_gib=size,
                             node_affinity=volume.get("nodeAffinity")))
    receipt = dict(phase=phase, reservation_id=reservation, bindings=bindings,
                   project_id=project, cluster_id=cluster)
    if phase == "all":
        if not db_receipt or any(db_receipt.get(key) != receipt[key]
                                 for key in ("reservation_id", "project_id", "cluster_id")) or (
            db_receipt.get("phase"), db_receipt.get("bindings")
        ) != ("db", bindings[:1]):
            raise ValueError("Expected the unchanged first-stage database binding receipt")
        receipt["baseline"] = dict(project_id=project, cluster_id=cluster, group_ids=[], instance_ids=[],
                                   disk_ids=sorted(handles))
    return receipt


def query(argv):
    return json.loads(subprocess.run(argv, capture_output=True, check=True, timeout=40).stdout)


def capture(args, approval, approval_hash):
    project, cluster, tenant = (approval[key] for key in ("project_id", "cluster_id", "tenant_id"))
    k = ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context, "--request-timeout=15s"]
    n = ["nebius", "-p", args.profile, "--no-browser"]
    pinned_kubeconfig_ca(k, cluster, args.ca_sha256, endpoint=args.endpoint, direct=True)
    target = query([*n, "mk8s", "cluster", "get", "--id", cluster, "--format", "json"])
    if target["metadata"]["parent_id"] != project or target["status"]["state"] != "RUNNING":
        raise ValueError("Unexpected managed cluster owner or state")
    owner = query([*n, "iam", "project", "get", "--id", project, "--format", "json"])
    if owner["metadata"]["parent_id"] != tenant or owner["status"]["region"] != "eu-south1":
        raise ValueError("Expected the approved eu-south1 tenant and workload project")
    quota = query([*n, "quotas", "quota-allowance", "get-by-name", "--parent-id", tenant,
                   "--name", "compute.disk.count", "--region", "eu-south1", "--format", "json"])
    quota_limit = int(quota["spec"]["limit"])
    if quota_limit < 5:
        raise ValueError("The combined topology requires an effective five-disk quota")
    if (cloud_items(args.profile, "mk8s", "node-group", cluster)
            or cloud_items(args.profile, "compute", "instance", project)):
        raise ValueError("Expected zero managed worker groups and Compute instances")
    disks = list(cloud_items(args.profile, "compute", "disk", project).values())
    inventory = query([*k, "get", "storageclasses,pv,pvc,nodes,csinodes,volumeattachments,csidrivers", "-A", "-o", "json"])
    if inventory.get("metadata", {}).get("continue"):
        raise ValueError("Incomplete Kubernetes inventory")
    kinds = {}
    for row in inventory["items"]:
        kinds.setdefault(row["kind"], []).append(row)
    if kinds.get("Node") or kinds.get("CSINode"):
        raise ValueError("Expected zero Kubernetes nodes")
    drivers = [row for row in kinds.get("CSIDriver", []) if row["metadata"]["name"] == DRIVER]
    if len(drivers) != 1 or drivers[0]["spec"].get("attachRequired") is not True:
        raise ValueError("Expected the registered managed Compute CSI driver")
    classes = [row for row in kinds.get("StorageClass", []) if row["metadata"]["name"] == CLASS]
    claims, volumes, attachments = (kinds.get(kind, []) for kind in
                                     ("PersistentVolumeClaim", "PersistentVolume", "VolumeAttachment"))
    held = funding(approval, datetime.now(timezone.utc))
    namespaces = query([*k, "get", "namespace", "tracking", "training", "-o", "json"])
    namespace_uids = {row["metadata"]["name"]: row["metadata"]["uid"] for row in namespaces["items"]}
    if set(namespace_uids) != {"tracking", "training"} or not all(namespace_uids.values()):
        raise ValueError("Expected the canonical training and tracking namespace identities")
    receipt = dict(observed_at=datetime.now(timezone.utc).isoformat(), approval_sha256=approval_hash,
                   namespace_uids=namespace_uids,
                   phase=args.phase, project_id=project, cluster_id=cluster, disk_quota_limit=quota_limit,
                   priced_storage_bound_usd=str(held))
    if args.phase == "preflight":
        if classes or claims or volumes or disks or attachments:
            raise ValueError("Existing claims or storage require a separately reviewed compatibility plan")
        workloads = query([*k, "get", "deployments,daemonsets,statefulsets,pods", "-A", "-o", "json"])
        receipt["visible_csi_workloads"] = [
            {"kind": row["kind"], "namespace": row["metadata"]["namespace"],
             "name": row["metadata"]["name"], "ready": row.get("status", {}).get(
                 "readyReplicas", row.get("status", {}).get("numberReady", 0))}
            for row in workloads["items"] if "csi" in row["metadata"]["name"].lower()
        ]
        return receipt
    if len(classes) != 1:
        raise ValueError("Expected exactly one funded Immediate StorageClass")
    first = json.loads(args.db_receipt.read_bytes()) if args.db_receipt else None
    if first is not None and (first.get("approval_sha256"), first.get("namespace_uids")) != (approval_hash, namespace_uids):
        raise ValueError("First-stage receipt belongs to a different funding approval")
    if args.phase == "db":
        if len(claims) != 1:
            raise ValueError("The database must be the only first-stage claim")
        created = utc(claims[0]["metadata"]["creationTimestamp"])
        now = datetime.now(timezone.utc)
        if not utc(approval["approved_at"]) <= created <= now <= created + timedelta(minutes=5):
            raise ValueError("The first worker-free database bind missed its five-minute evidence window")
    return receipt | reconcile(args.phase, approval, classes[0], claims, volumes, disks, attachments, first)


def write(path, obj):
    raw = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(raw)
    return hashlib.sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "db", "all"))
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--db-receipt", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--endpoint", choices=("public", "private"), required=True)
    parser.add_argument("--ca-sha256", required=True)
    parser.add_argument("--profile", default="ml-prod")
    args = parser.parse_args()
    if (args.phase == "all") != bool(args.baseline) or (args.phase == "all") != bool(args.db_receipt):
        parser.error("Only all-stage capture requires both an earlier DB receipt and a new baseline path")
    os.environ.setdefault("AI_AGENT", "codex")
    raw = args.approval.read_bytes()
    approval = json.loads(raw)
    try:
        funding(approval, datetime.now(timezone.utc))
        receipt = capture(args, approval, hashlib.sha256(raw).hexdigest())
        if args.baseline:
            receipt["baseline_sha256"] = write(args.baseline, receipt["baseline"])
        digest = write(args.output, receipt)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    print(f"Verified {args.phase} storage evidence SHA-256 {digest}")


if __name__ == "__main__":
    main()
