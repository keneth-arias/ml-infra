#!/usr/bin/env python3
"""Submit reviewed retained claims and capture exact managed storage evidence."""

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
CLAIMS = {("tracking", "postgres"): 16, ("training", "airaware-checkpoints"): 128}
GIB = 1024**3
WORKER_BOOT_DISKS = 2
WORKER_BOOT_GIB = 128
DB = ("tracking", "postgres")
TOPOLOGIES = [{"matchLabelExpressions": [{"key": "kubernetes.io/os", "values": ["linux"]}]}]
REPAIR_ATTEMPT = "airaware.storage/topology-repair-attempt"
REPAIR_STARTED = "airaware.storage/topology-repair-at"


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


def require_class(storage_class, *, allow_legacy=False):
    if storage_class["metadata"]["name"] != CLASS or (
        storage_class["provisioner"], storage_class["volumeBindingMode"], storage_class["reclaimPolicy"],
        storage_class["parameters"],
    ) != (DRIVER, "Immediate", "Retain", {"type": "NETWORK_SSD", "csi.storage.k8s.io/fstype": "ext4"}):
        raise ValueError("The retained StorageClass differs from the reviewed manifest")
    topologies = storage_class.get("allowedTopologies")
    if topologies != TOPOLOGIES and not (allow_legacy and topologies is None):
        raise ValueError("The retained StorageClass differs from the reviewed CSI topology")


def reconcile(phase, approval, storage_class, claims, volumes, disks, attachments, db_receipt=None, *, layout=CLAIMS):
    require_class(storage_class, allow_legacy=True)
    expected = {DB: layout[DB]} if phase == "db" else layout
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
        if (meta.get("deletionTimestamp") or claim.get("status", {}).get("phase") != "Bound" or pv_name not in volumes
                or meta.get("labels", {}).get(OWNER) != reservation or not meta.get("uid")
                or spec["storageClassName"] != CLASS or spec["accessModes"] != ["ReadWriteOnce"]
                or spec.get("volumeMode", "Filesystem") != "Filesystem"
                or spec["resources"]["requests"]["storage"] != f"{size}Gi"):
            raise ValueError("An approved final claim is not Bound to its reviewed storage class")
        pv = volumes[pv_name]
        volume = pv["spec"]
        ref, csi = volume.get("claimRef", {}), volume.get("csi", {})
        handle = csi.get("volumeHandle", "")
        if (pv["metadata"].get("deletionTimestamp") or not pv["metadata"].get("uid") or pv.get("status", {}).get("phase") != "Bound"
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
                or status.get("reconciling") or status.get("managed_by") or status.get("read_write_attachment")
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


def kube(args, timeout=15):
    return ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", args.context,
            f"--request-timeout={timeout}s"]


def observe(args, approval):
    project, cluster, tenant = (approval[key] for key in ("project_id", "cluster_id", "tenant_id"))
    k = kube(args)
    n = ["nebius", "-p", args.profile, "--no-browser"]
    pinned_kubeconfig_ca(k, cluster, args.ca_sha256, endpoint=args.endpoint, direct=True)
    target = query([*n, "mk8s", "cluster", "get", "--id", cluster, "--format", "json"])
    if target["metadata"]["parent_id"] != project or target["status"]["state"] != "RUNNING":
        raise ValueError("Unexpected managed cluster owner or state")
    owner = query([*n, "iam", "project", "get", "--id", project, "--format", "json"])
    if owner["metadata"]["parent_id"] != tenant or owner["status"]["region"] != "eu-south1":
        raise ValueError("Expected the approved eu-south1 tenant and workload project")
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
    namespaces = query([*k, "get", "namespace", "tracking", "training", "-o", "json"])
    namespace_uids = {row["metadata"]["name"]: row["metadata"]["uid"] for row in namespaces["items"]}
    if (set(namespace_uids) != {"tracking", "training"} or not all(namespace_uids.values())
            or any(row["metadata"].get("deletionTimestamp") for row in namespaces["items"])):
        raise ValueError("Expected the canonical, active training and tracking namespaces")
    return dict(storage_classes=classes, claims=kinds.get("PersistentVolumeClaim", []),
                volumes=kinds.get("PersistentVolume", []), disks=disks,
                attachments=kinds.get("VolumeAttachment", []), namespace_uids=namespace_uids,
                project_id=project, cluster_id=cluster)


def evidence(path, phase, receipt, approval, *, trusted_digest=None, require_digest=False):
    raw = path.read_bytes()
    if (require_digest or trusted_digest is not None) and hashlib.sha256(raw).hexdigest() != trusted_digest:
        raise ValueError("Evidence bytes differ from the independently verified receipt digest")
    value = json.loads(raw)
    if (value.get("phase") != phase or any(value.get(key) != receipt[key] for key in
                                         ("approval_sha256", "namespace_uids", "project_id", "cluster_id"))
            or not utc(approval["approved_at"]) <= utc(value["observed_at"]) <= utc(receipt["observed_at"])):
        raise ValueError("Storage evidence belongs to a different approval, target, phase or time")
    return value


def object_key(row):
    return (row["kind"], row["metadata"].get("namespace", ""), row["metadata"]["name"])


def submitted_uids(submission, objects, expected):
    records = {tuple(row["key"]): row["uid"] for row in submission["created_objects"]}
    if (set(records) != expected or len(records) != len(submission["created_objects"])
            or records != {object_key(row): row["metadata"]["uid"] for row in objects}):
        raise ValueError("Live claim identities differ from the create-only submission receipt")


def repair_start(repair, receipt, approval, storage_class, db_claim):
    annotations = storage_class["metadata"].get("annotations", {})
    marker = annotations.get(REPAIR_ATTEMPT), annotations.get(REPAIR_STARTED)
    if repair is None:
        if REPAIR_ATTEMPT in annotations or REPAIR_STARTED in annotations:
            raise ValueError("The storage topology repair requires independently verified evidence")
        return None
    expected = {key: receipt[key] for key in ("approval_sha256", "cluster_id", "project_id")}
    expected.update(reservation_id=approval["reservation_id"], namespace_uid=receipt["namespace_uids"]["tracking"],
                    pvc_uid=db_claim["metadata"]["uid"], storage_class_uid=storage_class["metadata"]["uid"])
    if (set(repair) != {*expected, "attempt_id", "started_at"}
            or any(repair[key] != value for key, value in expected.items())
            or marker != (repair["attempt_id"], repair["started_at"])
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", repair["attempt_id"])):
        raise ValueError("Storage topology repair differs from the funded original database claim")
    started = utc(repair["started_at"])
    if not max(utc(row["metadata"]["creationTimestamp"]) for row in (storage_class, db_claim)) < started:
        raise ValueError("Storage topology repair must follow creation of both original objects")
    return started


def check_db_window(started, observed, approval, volumes, disks, binding, *, repaired):
    if not started <= observed <= started + timedelta(minutes=5) or observed >= utc(approval["acquisition_deadline"]):
        raise ValueError("The first worker-free database bind missed its five-minute evidence window")
    if repaired:
        pv = next(row for row in volumes if row["metadata"]["name"] == binding["pv"])
        disk = next(row for row in disks if row["metadata"]["id"] == binding["provider_disk_id"])
        pv_created = utc(pv["metadata"]["creationTimestamp"])
        disk_created = utc(disk["metadata"]["created_at"])
        # Kubernetes timestamps have whole-second precision; Nebius can record subsecond creation.
        if not started <= disk_created <= observed or not disk_created.replace(microsecond=0) <= pv_created <= observed:
            raise ValueError("The repaired database PV and disk must originate after the topology patch")


def quota_headroom(n, tenant, name, needed, owned, *, unit=None):
    quota = query([*n, "quotas", "quota-allowance", "get-by-name", "--parent-id", tenant,
                   "--name", name, "--region", "eu-south1", "--format", "json"])
    spec, status = quota["spec"], quota["status"]
    if unit is not None and status.get("unit") != unit:
        raise ValueError(f"Unexpected unit for quota {name}")
    limit = int(spec["limit"])
    if status.get("usage") is None and status.get("usage_state") != "USAGE_STATE_NOT_USED":
        raise ValueError(f"Nebius did not report usage for quota {name}")
    usage = int(status.get("usage") or 0)
    if usage < owned:
        raise ValueError(f"Reported quota usage for {name} is below the verified disk inventory")
    if limit < needed or usage > limit - needed + owned:
        raise ValueError(f"Insufficient eu-south1 quota for the combined topology: {name}")
    return limit


def disk_quota(n, tenant, owned_disks, owned_gib):
    count = len(CLAIMS) + WORKER_BOOT_DISKS
    capacity = (sum(CLAIMS.values()) + WORKER_BOOT_DISKS * WORKER_BOOT_GIB) * GIB
    return {
        "disk_quota_limit": quota_headroom(n, tenant, "compute.disk.count", count, owned_disks),
        "disk_quota_bytes_limit": quota_headroom(
            n, tenant, "compute.disk.size.network-ssd", capacity, owned_gib * GIB, unit="byte",
        ),
    }


def capture(args, approval, approval_hash):
    phase = {"submit-db": "preflight", "submit-training": "db-live"}.get(args.phase, args.phase)
    state = observe(args, approval)
    n = ["nebius", "-p", args.profile, "--no-browser"]
    disks = state["disks"]
    owned_gib = sum(int(disk["spec"]["size_gibibytes"]) for disk in disks)
    quota = disk_quota(n, approval["tenant_id"], len(disks), owned_gib)
    if state["namespace_uids"] != approval["namespace_uids"]:
        raise ValueError("The namespace identities differ from the approved scope")
    now = datetime.now(timezone.utc)
    receipt = {key: state[key] for key in ("namespace_uids", "project_id", "cluster_id")}
    receipt.update(observed_at=now.isoformat(), approval_sha256=approval_hash, phase=phase,
                   priced_storage_bound_usd=str(funding(approval, now)), **quota)
    classes, claims, volumes, disks, attachments = (state[key] for key in
        ("storage_classes", "claims", "volumes", "disks", "attachments"))
    repair = getattr(args, "db_repair", None)
    if phase == "preflight":
        if repair is not None:
            raise ValueError("A topology repair must reference an existing paid database submission")
        if classes or claims or volumes or disks or attachments:
            raise ValueError("Existing claims or storage require a separately reviewed compatibility plan")
        return receipt
    if (len(classes) != 1 or classes[0]["metadata"].get("deletionTimestamp")
            or classes[0]["metadata"].get("labels", {}).get(OWNER) != approval["reservation_id"]):
        raise ValueError("Expected exactly one active funded Immediate StorageClass")
    require_class(classes[0])
    start = utc(approval["approved_at"])
    if any(not start <= utc(row["metadata"]["creationTimestamp"]) <= now for row in [*classes, *claims, *volumes]):
        raise ValueError("Kubernetes storage evidence was created outside this acquisition")
    if any(not start <= utc(row["metadata"]["created_at"]) <= now for row in disks):
        raise ValueError("Provider disk evidence was created outside this acquisition")
    db_claims = [row for row in claims if object_key(row) == ("PersistentVolumeClaim", *DB)]
    if len(db_claims) != 1:
        raise ValueError("Expected exactly one final database claim")
    db_created = utc(db_claims[0]["metadata"]["creationTimestamp"])
    db_objects = [*classes, *db_claims]
    db_keys = {("StorageClass", "", CLASS), ("PersistentVolumeClaim", *DB)}
    repaired_at = repair_start(repair, receipt, approval, classes[0], db_claims[0])
    window_start = repaired_at or db_created
    if phase == "db":
        submission = evidence(args.submission, "submit-db", receipt, approval)
        submitted_uids(submission, db_objects, db_keys)
        if not db_created <= utc(submission["observed_at"]) <= (repaired_at or now):
            raise ValueError("The original paid database submission does not precede evidence capture or repair")
        live = reconcile("db", approval, classes[0], claims, volumes, disks, attachments)
        check_db_window(window_start, now, approval, volumes, disks, live["bindings"][0], repaired=repair is not None)
        receipt["db_objects"] = submission["created_objects"]
        if repair is not None:
            receipt["db_repair"] = repair
        return receipt | live
    first = evidence(args.db_receipt, "db", receipt, approval,
                     trusted_digest=getattr(args, "db_receipt_sha256", None), require_digest=repair is not None)
    if first.get("db_repair") != repair:
        raise ValueError("The first database receipt differs from the verified topology repair")
    submitted_uids({"created_objects": first["db_objects"]}, db_objects, db_keys)
    if phase == "db-live":
        live = reconcile("db", approval, classes[0], claims, volumes, disks, attachments)
        if live["bindings"] != first["bindings"]:
            raise ValueError("The first database binding changed before training submission")
    else:
        live = reconcile("all", approval, classes[0], claims, volumes, disks, attachments, first)
    check_db_window(window_start, utc(first["observed_at"]), approval, volumes, disks, live["bindings"][0],
                    repaired=repair is not None)
    if repair is not None:
        receipt["db_repair"] = repair
    if phase == "db-live":
        return receipt
    training = [row for row in claims if row not in db_claims]
    submission = evidence(args.submission, "submit-training", receipt, approval)
    if utc(submission["observed_at"]) < utc(first["observed_at"]):
        raise ValueError("Training submission predates the database qualification")
    submitted_uids(submission, training, {("PersistentVolumeClaim", *key) for key in CLAIMS if key != DB})
    if submission.get("db_repair") != repair:
        raise ValueError("Training submission differs from the verified topology repair")
    return receipt | live


def submission_seconds(approval, checked_at, now):
    limit = min(utc(approval["acquisition_deadline"]), checked_at + timedelta(minutes=1))
    remaining = int((limit - now).total_seconds())
    if remaining < 1:
        raise ValueError("Storage submission approval or live guard checks expired")
    return min(15, remaining)


def create_object(args, obj, seconds):
    completed = subprocess.run([*kube(args, seconds), "create", "--validate=strict", "-f", "-", "-o", "json"],
                               input=json.dumps(obj).encode(), capture_output=True, check=True, timeout=seconds)
    created = json.loads(completed.stdout)
    if (object_key(created) != object_key(obj) or not created["metadata"].get("uid")
            or created["metadata"].get("deletionTimestamp")
            or created["metadata"].get("labels", {}).get(OWNER) != obj["metadata"]["labels"][OWNER]):
        raise ValueError("Create-only response differs from the scoped manifest")
    return {"key": object_key(created), "uid": created["metadata"]["uid"]}


def submit(args, approval, approval_hash):
    checked_at = datetime.now(timezone.utc)
    receipt = capture(args, approval, approval_hash)
    import yaml

    phase = "db" if args.phase == "submit-db" else "training"
    manifest = subprocess.check_output(["kubectl", "kustomize", Path(__file__).parent / phase], timeout=10)
    items = list(yaml.safe_load_all(manifest))
    expected = ([("StorageClass", "", CLASS), ("PersistentVolumeClaim", *DB)] if phase == "db" else
                [("PersistentVolumeClaim", *key) for key in CLAIMS if key != DB])
    actual = indexed(items, object_key)
    if set(actual) != set(expected):
        raise ValueError("Rendered objects differ from the reviewed stage identities")
    for key in expected:
        obj = actual[key]
        if key[0] == "StorageClass":
            require_class(obj)
            continue
        storage = {"accessModes": ["ReadWriteOnce"], "storageClassName": CLASS,
                   "resources": {"requests": {"storage": f"{CLAIMS[key[1:]]}Gi"}}}
        if obj["spec"] != storage:
            raise ValueError("Rendered claim differs from the funded blank storage specification")
    created = []
    for key in expected:
        item = actual[key]
        item["metadata"].setdefault("labels", {})[OWNER] = approval["reservation_id"]
        seconds = submission_seconds(approval, checked_at, datetime.now(timezone.utc))
        k = kube(args, seconds)
        pinned_kubeconfig_ca(k, approval["cluster_id"], args.ca_sha256, endpoint=args.endpoint, direct=True)
        subject = query([*k, "auth", "whoami", "-o", "json"])["status"]["userInfo"]["username"]
        if subject != approval["operator_username"]:
            raise ValueError("Kubernetes operator differs from the approval")
        if key[1]:
            namespace = query([*k, "get", "namespace", key[1], "-o", "json"])["metadata"]
            if namespace["uid"] != approval["namespace_uids"][key[1]] or namespace.get("deletionTimestamp"):
                raise ValueError("The approved claim namespace changed before submission")
        # Only the training stage has an already-verified disk, the database.
        owned = int(phase == "training")
        disk_quota(["nebius", "-p", args.profile, "--no-browser"], approval["tenant_id"], owned, owned * CLAIMS[DB])
        funding(approval, datetime.now(timezone.utc))
        seconds = submission_seconds(approval, checked_at, datetime.now(timezone.utc))
        created.append(create_object(args, item, seconds))
    return receipt | dict(phase=args.phase, observed_at=datetime.now(timezone.utc).isoformat(), created_objects=created)


def private_output(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def write_receipt(out, obj):
    raw = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()
    out.write(raw)
    out.flush()
    os.fsync(out.fileno())
    return hashlib.sha256(raw).hexdigest()


def write(path, obj):
    with private_output(path) as out:
        return write_receipt(out, obj)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("preflight", "submit-db", "db", "submit-training", "all"))
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--approval-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--db-receipt", type=Path)
    parser.add_argument("--submission", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--endpoint", choices=("public", "private"), required=True)
    parser.add_argument("--ca-sha256", required=True)
    parser.add_argument("--profile", default="ml-prod")
    args = parser.parse_args()
    if ((args.phase == "all") != bool(args.baseline)
            or (args.phase in ("submit-training", "all")) != bool(args.db_receipt)
            or (args.phase in ("db", "all")) != bool(args.submission)):
        parser.error("Specify the stage's required prior receipts and baseline output")
    if args.output.exists() or (args.baseline and (args.baseline == args.output or args.baseline.exists())):
        parser.error("Choose fresh private output paths")
    os.environ.setdefault("AI_AGENT", "codex")
    raw = args.approval.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.approval_sha256:
        parser.exit(1, "Approval bytes differ from the independently reviewed digest\n")
    approval = json.loads(raw)
    try:
        funding(approval, datetime.now(timezone.utc))
        if args.phase.startswith("submit-"):
            # Reserve the private receipt before a paid request. An empty file records an incomplete stage.
            with private_output(args.output) as out:
                digest = write_receipt(out, submit(args, approval, digest))
        else:
            receipt = capture(args, approval, digest)
            if args.baseline:
                receipt["baseline_sha256"] = write(args.baseline, receipt["baseline"])
            digest = write(args.output, receipt)
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        parser.exit(1, "A guarded operation failed or timed out. Reconcile exact live objects before retrying.\n")
    print(f"Verified {args.phase} storage evidence SHA-256 {digest}")


if __name__ == "__main__":
    main()
