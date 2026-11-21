"""Consume a single guarded worker create in the versioned off-host budget ledger."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from pathlib import Path

from worker_cleanup import cleanup_digest, scope_digest

from gpu_window import BID, RELEASE_HOURS, bounds, hours, pilot_bounds
from worker_plan import amount, timestamp

LEDGER_KEY = "prod/mk8s/release-control/ledger.json"


def reserve(s3, scope, plan_digest, state_serial, now):
    bucket = scope["state_bucket"]
    run, attempt, stage = (scope[k] for k in ("run_id", "attempt_id", "stage"))
    for identifier in (run, attempt):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", identifier):
            raise ValueError("Invalid run or attempt ID")
    if stage not in ("pilot", "support", "gpu") or not re.fullmatch(
        "[0-9a-f]{64}", scope["baseline_sha256"]
    ):
        raise ValueError("Invalid stage or inventory digest")
    current = s3.get_object(Bucket=bucket, Key=LEDGER_KEY)
    if not isinstance(current.get("VersionId"), str) or current["VersionId"] in ("", "null"):
        raise ValueError("Funding ledger requires a versioned object")
    raw = current["Body"].read()
    if hashlib.sha256(raw).hexdigest() != scope["ledger_sha256"]:
        raise ValueError("Live ledger changed since approval")
    ledger = json.loads(raw)
    gpu_hours = 0
    if stage == "pilot":
        pilot_bounds(scope, ledger, now)
    else:
        limits = bounds(scope, ledger, now)
        gpu_hours = hours(timestamp(scope["cutoff"]) - now) + RELEASE_HOURS
        if gpu_hours - RELEASE_HOURS > min(
            limits["dollar_limit_hours"], limits["hour_limit"]
        ):
            raise ValueError("Full remaining allocation exceeds the reservation")
    if ledger.get("active_reservation") or attempt in ledger.get("attempt_ids", []):
        raise ValueError(
            "Existing or previously consumed reservation; reconcile before retry"
        )
    accounts = scope["cleanup_accounts"]
    if (
        set(accounts) != {"bm", "fw13"}
        or not all(accounts.values())
        or len(set(accounts.values())) != 2
    ):
        raise ValueError("Expected distinct identities for both cleanup hosts")
    # The funding owner binds the IAM administrator's independent grant check
    # to the actual keys/identities and the whole period through release.
    grants = ledger["cleanup_access"]
    for host, account in accounts.items():
        grant = grants[host]
        if (
            grant["account_id"] != account
            or not grant["public_key_id"]
            or grant["state_bucket"] != bucket
            or grant["state_key"] != "prod/mk8s/terraform.tfstate"
            or grant["project_id"] != scope["project_id"]
            or grant["cluster_id"] != scope["cluster_id"]
            or not re.fullmatch("[0-9a-f]{64}", grant["evidence_sha256"])
            or not timestamp(grant["checked_at"]) <= now
            or timestamp(grant["valid_through"])
            < timestamp(scope["cutoff"]) + timedelta(hours=1)
            or any(
                grant.get(key) is not True
                for key in ("node_group_delete", "state_write", "lock_write_delete")
            )
        ):
            raise ValueError(
                f"{host} cleanup grant evidence does not cover this allocation"
            )
    root = Path(__file__).resolve().parent
    units = {
        name: root / "management" / name
        for name in (
            "worker-expiry@.timer",
            "worker-expiry@.service",
            "worker-attest@.service",
        )
    }
    expected_source = cleanup_digest(root, units)
    versions = {}
    for host, account in accounts.items():
        key = f"prod/mk8s/release-control/timers/{run}/{stage}/{host}.json"
        obj = s3.get_object(Bucket=bucket, Key=key)
        proof = json.loads(obj["Body"].read())
        expected = {
            k: scope[k]
            for k in (
                "stage",
                "run_id",
                "cutoff",
                "project_id",
                "cluster_id",
                "state_lineage",
                "baseline_sha256",
            )
        }
        expected.update(
            host=host,
            account_id=account,
            provider_key_id=grants[host]["public_key_id"],
            scope_sha256=scope_digest(scope),
            cleanup_source_sha256=expected_source,
            state_serial=state_serial,
            timer=f"worker-expiry@{run}.timer",
        )
        if any(proof.get(k) != v for k, v in expected.items()) or not all(
            proof.get(k) is True
            for k in (
                "enabled",
                "active",
                "cloud_state_access",
                "inventory_reconciled",
                "locked_release_plan_checked",
            )
        ):
            raise ValueError(f"{host} timer attestation mismatches the allocation")
        if (
            not now - timedelta(minutes=2)
            <= timestamp(proof["observed_at"])
            <= now
            < timestamp(proof["next_tick"])
            <= now + timedelta(seconds=65)
        ):
            raise ValueError(
                f"{host} timer attestation is stale or has no imminent tick"
            )
        version = obj.get("VersionId")
        if not version or version == "null":
            raise ValueError("Timer attestation requires a versioned object")
        versions[host] = version
    reservation = {
        "run_id": run,
        "attempt_id": attempt,
        "stage": stage,
        "plan_sha256": plan_digest,
        "prior_revision": ledger["revision"],
        "timer_versions": versions,
        "state_serial": state_serial,
        "gpu_hours": str(gpu_hours),
        "gpu_usd": str(gpu_hours * BID),
        "non_gpu_outstanding_usd": str(
            amount(scope["other_cost_usd"]) - amount(ledger["accrued_non_gpu_usd"])
        ),
        "reserved_at": now.astimezone(timezone.utc).isoformat(),
    }
    ledger.update(active_reservation=reservation, revision=uuid.uuid4().hex)
    ledger.setdefault("attempt_ids", []).append(attempt)
    # Competing clients reading the same revision race on one S3 object. Only
    # the successful writer may execute. On an ambiguous response leave the hold.
    result = s3.put_object(
        Bucket=bucket,
        Key=LEDGER_KEY,
        IfMatch=current["ETag"],
        ContentType="application/json",
        Body=json.dumps(ledger, sort_keys=True).encode(),
    )
    version = result.get("VersionId")
    if not isinstance(version, str) or version in ("", "null"):
        raise ValueError("Unversioned reservation write; inspect the off-host ledger before retry")
    return ledger["revision"], version


def reserve_remote(scope, plan_digest, state_serial):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    client = boto3.client(
        "s3",
        region_name="eu-south1",
        endpoint_url="https://storage.eu-south1.nebius.cloud",
        config=Config(
            connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}
        ),
    )
    try:
        return reserve(
            client,
            scope,
            plan_digest,
            state_serial,
            datetime.now(timezone.utc),
        )
    except (BotoCoreError, ClientError) as exc:
        raise ValueError(
            "Reservation unsuccessful or ambiguous; inspect the off-host ledger before retry"
        ) from exc
