#!/usr/bin/env python3
"""Wait for the exact production training RWO volume to have no consumer or attachment."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from time import monotonic, sleep

from private_api import pinned_kubeconfig_ca


CLAIM = "airaware-checkpoints"
STORAGE_CLASS = "airaware-network-retain-immediate"
SUBMITTER = "system:serviceaccount:training:episode-submit"


def status(get):
    claim = get("pvc", CLAIM, "-n", "training")
    claim_meta, claim_spec = claim["metadata"], claim["spec"]
    pv_name = claim_spec.get("volumeName")
    if (not pv_name or not claim_meta.get("uid") or claim_meta.get("deletionTimestamp")
            or claim["status"]["phase"] != "Bound" or claim_spec["storageClassName"] != STORAGE_CLASS
            or claim_spec.get("volumeMode", "Filesystem") != "Filesystem"
            or claim_spec["accessModes"] != ["ReadWriteOnce"]
            or claim_spec["resources"]["requests"]["storage"] != "128Gi"):
        raise ValueError("Expected the active funded 128 GiB training claim")
    pv = get("pv", pv_name)
    pv_meta, volume = pv["metadata"], pv["spec"]
    source, ref = volume.get("csi", {}), volume.get("claimRef", {})
    handle = source.get("volumeHandle", "")
    if (not pv_meta.get("uid") or pv_meta.get("deletionTimestamp") or pv["status"]["phase"] != "Bound"
            or (ref.get("namespace"), ref.get("name"), ref.get("uid")) != ("training", CLAIM, claim_meta["uid"])
            or volume["storageClassName"] != STORAGE_CLASS or volume["capacity"]["storage"] != "128Gi"
            or volume.get("volumeMode", "Filesystem") != "Filesystem"
            or volume["accessModes"] != ["ReadWriteOnce"] or volume["persistentVolumeReclaimPolicy"] != "Retain"
            or source.get("driver") != "compute.csi.nebius.com" or source.get("fsType") != "ext4"
            or not re.fullmatch(r"computedisk-[a-z0-9]+", handle)):
        raise ValueError("Expected the exact dedicated training disk binding")
    pods = get("pods", "-n", "training")
    attachments = get("volumeattachments")
    if pods.get("metadata", {}).get("continue") or attachments.get("metadata", {}).get("continue"):
        raise ValueError("Incomplete training-storage inventory")
    active = any(
        pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}
        and any(v.get("persistentVolumeClaim", {}).get("claimName") == CLAIM for v in pod["spec"].get("volumes", []))
        for pod in pods["items"]
    )
    attached = any(item["spec"]["source"].get("persistentVolumeName") == pv_name for item in attachments["items"])
    return (claim_meta["uid"], pv_meta["uid"], handle), not (active or attached)


def approved_binding(path, digest, cluster):
    if path.stat().st_mode & 0o077:
        raise ValueError("Training binding receipt must be private")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Training binding receipt differs from its approved digest")
    receipt = json.loads(raw)
    training = [item for item in receipt["bindings"] if (item["namespace"], item["claim"]) == ("training", CLAIM)]
    if receipt["phase"] != "all" or receipt["cluster_id"] != cluster or len(training) != 1:
        raise ValueError("Expected the funded training binding for this deployment")
    item = training[0]
    if item["size_gib"] != 128 or item["csi_handle"] != item["provider_disk_id"]:
        raise ValueError("Training receipt does not bind the consolidated disk")
    return item["pvc_uid"], item["pv_uid"], item["provider_disk_id"]


def main():
    profile = Path(os.environ["NEBIUS_STORAGE_KUBECONFIG"])
    identity = os.environ["NEBIUS_STORAGE_OPERATOR_IDENTITY"]
    if not identity or identity == SUBMITTER or profile.stat().st_mode & 0o077:
        raise ValueError("Select a private kubeconfig for the independently approved storage operator")
    k = ["kubectl", "--kubeconfig", str(profile), "--context", "prod-nebius"]
    pinned_kubeconfig_ca(k, os.environ["NEBIUS_TRAINING_CLUSTER_ID"], os.environ["NEBIUS_CA_BUNDLE_SHA256"],
                         endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"), direct=True)
    who = subprocess.check_output([*k, "--request-timeout=10s", "auth", "whoami", "-o",
                                  "jsonpath={.status.userInfo.username}"], text=True, timeout=12)
    if who != identity:
        raise ValueError("Unexpected managed Kubernetes storage operator identity")
    binding = approved_binding(Path(os.environ["NEBIUS_STORAGE_BINDING_RECEIPT"]),
                               os.environ["NEBIUS_STORAGE_BINDING_SHA256"],
                               os.environ["NEBIUS_TRAINING_CLUSTER_ID"])
    deadline = monotonic() + 120

    def read(*args):
        remaining = deadline - monotonic()
        if remaining < 1:
            raise ValueError("Timed out waiting for training storage to detach")
        timeout = min(int(remaining), 10)
        return json.loads(subprocess.check_output([*k, f"--request-timeout={timeout}s", "get", *args, "-o", "json"],
                                                  timeout=remaining))

    while monotonic() < deadline:
        current, idle = status(read)
        if current != binding:
            raise ValueError("Training disk binding changed during storage handoff")
        if idle:
            return
        sleep(2)
    raise ValueError("Timed out waiting for training storage to detach")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"Training storage handoff failed: {error}")
