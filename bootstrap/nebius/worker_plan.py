"""Checks shared by the pilot and GPU-window saved-plan guards."""

from datetime import datetime, timedelta
from decimal import Decimal


CPU = 'nebius_mk8s_v1_node_group.training["cpu"]'
GPU = 'nebius_mk8s_v1_node_group.training["gpu"]'


def timestamp(value):
    if not value.endswith("Z"):
        raise ValueError("Expected UTC timestamps ending in Z")
    return datetime.fromisoformat(value)


def amount(value):
    n = Decimal(str(value))
    if not n.is_finite() or n < 0:
        raise ValueError("Expected finite nonnegative ledger entries")
    return n


def pilot_budget(
    window,
    *,
    cpu_rate,
    accrued_non_gpu,
    outstanding_other,
    cleanup,
    gpu_used,
    gpu_remaining,
    gpu_spend,
    reserved_non_gpu,
):
    """Price the complete CPU window and release hour, preserving funding for later GPU work."""
    if not timedelta(0) < window <= timedelta(hours=8):
        raise ValueError("CPU pilot must fit an eight-hour window")
    rate, reserve = amount(cpu_rate), amount(cleanup)
    if min(rate, reserve) <= 0:
        raise ValueError("Price the CPU pilot and cleanup reserve")
    duration = Decimal(window // timedelta(microseconds=1)) / 3_600_000_000
    cpu_cost = (duration + 1) * rate
    non_gpu = amount(accrued_non_gpu) + amount(outstanding_other) + reserve + cpu_cost
    used, future, funded = (
        amount(gpu_used),
        amount(gpu_remaining),
        amount(reserved_non_gpu),
    )
    total = funded + amount(gpu_spend) + future * Decimal("1.50")
    if not non_gpu <= funded <= 100 or used + future > 100 or total > 250:
        raise ValueError("Ledger exceeds the approved spend or GPU hours")
    return {
        "cpu_and_boot_window_usd": cpu_cost,
        "projected_non_gpu_usd": non_gpu,
        "funded_total_usd": total,
        "gpu_hours_used": used,
        "future_gpu_hours": future,
    }


def compact(value):
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [compact(v) for v in value]
    return value


def unknowns(node, path=()):
    if node is True:
        yield ".".join(path)
    if isinstance(node, dict):
        for k, v in node.items():
            yield from unknowns(v, (*path, k))
    if isinstance(node, list):
        for item in node:
            yield from unknowns(item, path)


def check_envelope(plan):
    if (
        plan["format_version"].split(".")[0] != "1"
        or not plan["complete"]
        or plan["errored"]
        or plan.get("deferred_changes")
        or plan.get("action_invocations")
    ):
        raise ValueError("Expected a complete plan with no drift")
    for drift in plan.get("resource_drift", []):
        change = drift["change"]
        old, new = change.get("before") or {}, change.get("after") or {}
        changed = {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
        if (
            drift["address"] != "nebius_storage_v1_bucket.artifacts[0]"
            or change["actions"] != ["update"]
            or changed - {"status", "updated_at"}
        ):
            raise ValueError("Unexpected drift at " + drift["address"])


def check_gpu_mode(scope, ledger):
    mode = scope.get("gpu_mode", "native")
    if mode not in ("native", "hami") or ledger.get("gpu_mode", "native") != mode:
        raise ValueError("GPU mode differs from the approved funding ledger")
    if mode == "hami" and scope["stage"] == "pilot":
        raise ValueError("HAMi requires an approved support and GPU window")
    return mode


def check_group(change, pool, scope):
    hami = pool == "gpu" and scope.get("gpu_mode") == "hami"
    name = "training-hami" if hami else "training-" + pool
    after = compact(change["after"])
    expected_template = {
        "os": "ubuntu24.04",
        "resources": {"platform": "cpu-d3", "preset": "4vcpu-16gb"},
        "metadata": {"labels": {"airaware.training/pool": name}},
        "taints": [],
        "network_interfaces": [{"subnet_id": scope["subnet_id"]}],
        "boot_disk": {"type": "NETWORK_SSD", "size_gibibytes": 128},
    }
    if pool == "gpu":
        expected_template.update(
            resources={"platform": "gpu-rtx6000-a", "preset": "1gpu-24vcpu-218gb"},
            preemptible={},
            spot_pricing_policy={"id": scope["policy_id"]},
            taints=[
                {"key": "nvidia.com/gpu", "value": "true", "effect": "NO_SCHEDULE"}
            ],
        )
        if hami:
            expected_template["metadata"]["labels"] = {"airaware.training/pool": name}
            expected_template["taints"] = [
                {"key": "airaware.training/gpu-mode", "value": "hami", "effect": "NO_SCHEDULE"}
            ]
        else:
            expected_template["gpu_settings"] = {"drivers_preset": "cuda13.0"}
            expected_template["metadata"]["labels"]["skypilot.co/accelerator"] = "rtxpro6000"
    expected = {
        "parent_id": scope["cluster_id"],
        "fixed_node_count": 1,
        "name": name,
        "version": "1.36",
        "template": expected_template,
        "strategy": {
            "max_surge": {"count": 0},
            "max_unavailable": {"count": 1},
            "drain_timeout": "5m",
        },
        "labels": {
            "airaware-run": scope["run_id"],
            "airaware-cutoff": timestamp(scope["cutoff"]).strftime("%Y%m%d%H%M%S"),
        },
    }
    # v0.6.88 duplicates resource labels in labels_all.
    if (
        after.pop("labels_all", expected["labels"]) != expected["labels"]
        or after != expected
    ):
        raise ValueError(f"{pool} group differs from reviewed topology")
    generated = {
        "id",
        "created_at",
        "updated_at",
        "metadata",
        "resource_version",
        "status",
        "template.local_disks",
        "template.network_interfaces.public_ip_address",
        "template.network_interfaces.security_groups",
    }
    if not hami:
        generated.add("template.gpu_settings.dra" if pool == "gpu" else "template.gpu_settings")
    generated.update(
        "template.boot_disk." + field
        for field in (
            "block_size_bytes",
            "disk_encryption",
            "size_bytes",
            "size_kibibytes",
            "size_mebibytes",
        )
    )
    if set(unknowns(change.get("after_unknown", {}))) - generated:
        raise ValueError("Unreviewed unknown field")
