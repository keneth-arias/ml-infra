#!/usr/bin/env python3
"""Read back the original, detached retained disks after the last worker is released."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import check as storage


LEGACY_SPLIT_CLAIMS = {("tracking", "postgres"): 16, ("training", "airaware-checkpoints"): 64,
                       ("training", "airaware-model-state"): 64}


def load_pinned(path, expected_sha256):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(f"Archived evidence digest differs for {path.name}")
    return json.loads(raw)


def verify_closeout(approval, original, baseline, live):
    if original["phase"] != "all" or baseline != original["baseline"]:
        raise ValueError("Expected the complete original acquisition and worker baseline")
    for field in ("project_id", "cluster_id", "namespace_uids"):
        if live[field] != original[field]:
            raise ValueError(f"Retained storage target changed: {field}")
    if len(live["storage_classes"]) != 1 or any(row["status"].get("managed_by") for row in live["disks"]):
        raise ValueError("Expected the retained StorageClass and independently owned disks")

    archived_layout = {
        (binding["namespace"], binding["claim"]): binding["size_gib"] for binding in original["bindings"]
    }
    if (len(archived_layout) != len(original["bindings"])
            or archived_layout not in (storage.CLAIMS, LEGACY_SPLIT_CLAIMS)):
        raise ValueError("The archived acquisition has an unrecognized retained storage layout")
    # The final acquisition receipt already pins the first database binding.
    first = {key: original[key] for key in ("project_id", "cluster_id", "reservation_id")}
    first["phase"] = "db"
    first["bindings"] = [b for b in original["bindings"] if (b["namespace"], b["claim"]) == storage.DB]
    verified = storage.reconcile("all", approval, live["storage_classes"][0], live["claims"], live["volumes"],
                                 live["disks"], live["attachments"], first,
                                 layout=storage.CLAIMS if archived_layout == storage.CLAIMS else LEGACY_SPLIT_CLAIMS)
    if verified["baseline"] != baseline or verified["bindings"] != original["bindings"]:
        raise ValueError("Live retained volumes differ from the original binding identities")
    return verified | dict(phase="worker-release", namespace_uids=live["namespace_uids"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-receipt", required=True, type=Path)
    parser.add_argument("--all-receipt-sha256", required=True)
    parser.add_argument("--approval", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--endpoint", choices=("private", "public"), required=True)
    parser.add_argument("--ca-sha256", required=True)
    parser.add_argument("--profile", default="ml-prod")
    args = parser.parse_args()
    os.environ.setdefault("AI_AGENT", "codex")
    try:
        original = load_pinned(args.all_receipt, args.all_receipt_sha256)
        approval = load_pinned(args.approval, original["approval_sha256"])
        baseline = load_pinned(args.baseline, original["baseline_sha256"])
        proof = verify_closeout(approval, original, baseline, storage.observe(args, approval))
        proof.update(observed_at=datetime.now(timezone.utc).isoformat(), acquisition_sha256=args.all_receipt_sha256,
                     baseline_sha256=original["baseline_sha256"])
        digest = storage.write(args.output, proof)
    except (KeyError, ValueError) as error:
        parser.exit(1, f"Retained storage closeout failed: {error}\n")
    print(f"Verified {len(proof['bindings'])} original detached disks. Release receipt SHA-256 {digest}")


if __name__ == "__main__":
    main()
