"""Allocated GPU time, cost and cutoff checks for the selected fit."""

from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import re

from worker_plan import (
    CPU,
    GPU,
    amount,
    check_envelope,
    check_group,
    check_gpu_mode,
    compact,
    pilot_budget,
    timestamp,
    unknowns,
)

BID = Decimal("1.50")
RELEASE_HOURS = Decimal(1)
MAX_AGE = timedelta(minutes=15)


def hours(delta):
    return Decimal(delta // timedelta(microseconds=1)) / 3_600_000_000


def gpu_time(intervals, as_of, now):
    """Return allocated hours and new unposted hours since the ledger observation."""
    by_vm = defaultdict(list)
    for interval in intervals:
        start = timestamp(interval["start"])
        end = now if interval["end"] is None else timestamp(interval["end"])
        vm = interval["vm"]
        if not isinstance(vm, str) or not vm.strip() or not start <= end <= now:
            raise ValueError("Invalid GPU allocation interval")
        by_vm[vm].append((start, end))
    total = extra = timedelta(0)
    for spans in by_vm.values():
        previous_end = None
        for start, end in sorted(spans):
            if previous_end is not None:
                start = max(start, previous_end)
            if start < end:
                total += end - start
                extra += max(timedelta(0), end - max(start, as_of))
            previous_end = max(end, previous_end or end)
    return hours(total), hours(extra)


class BudgetStop(ValueError):
    def __init__(self, reason, limits):
        super().__init__(reason)
        self.limits = limits


def pilot_bounds(scope, ledger, now):
    start, cutoff = timestamp(scope["start"]), timestamp(scope["cutoff"])
    as_of = timestamp(ledger["observed_at"])
    if (
        not start <= now < cutoff
        or not now - MAX_AGE <= as_of <= now
        or not ledger["revision"]
        or ledger["run_id"] != scope["run_id"]
    ):
        raise ValueError(
            "CPU pilot window closed or ledger stale or for a different run"
        )
    spans = ledger["gpu_intervals"]
    if any(span["end"] is None for span in spans):
        raise ValueError("Existing GPU allocation is still open")
    used, unposted = gpu_time(spans, as_of, now)
    outstanding = amount(ledger["other_remaining_usd"]) + amount(
        ledger["tax_remaining_usd"]
    )
    return pilot_budget(
        cutoff - start,
        cpu_rate=ledger["cpu_and_boot_hourly_usd"],
        accrued_non_gpu=ledger["accrued_non_gpu_usd"],
        outstanding_other=outstanding + amount(ledger.get("post_release_usd", 0)),
        cleanup=ledger["cleanup_other_usd"],
        gpu_used=used,
        gpu_remaining=ledger["remaining_gpu_hours"],
        gpu_spend=amount(ledger["accrued_gpu_usd"]) + unposted * BID,
        reserved_non_gpu=scope["other_cost_usd"],
    )


def bounds(scope, ledger, now):
    as_of = timestamp(ledger["observed_at"])
    start, cutoff = timestamp(scope["start"]), timestamp(scope["cutoff"])
    if not start <= now < cutoff or cutoff - start > timedelta(hours=99):
        raise ValueError("Invalid GPU window")
    if (
        not now - MAX_AGE <= as_of <= now
        or not ledger["revision"]
        or ledger["run_id"] != scope["run_id"]
    ):
        raise ValueError("Stale ledger or wrong run")
    used, unposted = gpu_time(ledger["gpu_intervals"], as_of, now)
    cpu_rate = amount(ledger["cpu_and_boot_hourly_usd"])
    disk_rate = amount(ledger["gpu_boot_hourly_usd"])
    grace = amount(ledger["drain_hours"])
    cleanup_other = amount(ledger["cleanup_other_usd"])
    if min(cpu_rate, disk_rate, grace, cleanup_other) <= 0:
        raise ValueError("Price the CPU, disks and cleanup and reserve drain time")
    non_gpu = (
        sum(
            amount(ledger[k])
            for k in ("accrued_non_gpu_usd", "other_remaining_usd", "tax_remaining_usd")
        )
        + cleanup_other
    )
    # The ledger already accrued charges through as_of. Include support, both
    # boot disks and their post-cutoff release exposure in the remaining period.
    non_gpu += (hours(cutoff - max(start, as_of)) + RELEASE_HOURS) * (
        cpu_rate + disk_rate
    )
    non_gpu += amount(ledger.get("post_release_usd", 0))
    budgeted = amount(scope["other_cost_usd"])
    spent = amount(ledger["accrued_gpu_usd"]) + unposted * BID
    limits = {
        "gpu_hours_used": used,
        "projected_non_gpu_usd": non_gpu,
        "hour_limit": 100 - used - RELEASE_HOURS,
        "dollar_limit_hours": (250 - budgeted - spent - RELEASE_HOURS * BID) / BID,
        "cutoff_limit_hours": hours(cutoff - now) - grace,
    }
    if not non_gpu <= budgeted <= 100:
        raise BudgetStop(
            "Insufficient non-GPU reservation or $100 ceiling exceeded", limits
        )
    if (
        min(
            limits[k]
            for k in ("hour_limit", "dollar_limit_hours", "cutoff_limit_hours")
        )
        <= 0
    ):
        raise BudgetStop("Allocation, money or cutoff exhausted", limits)
    return limits


def check_vm_funding(scope, limits, now):
    # The installed cutoff governs the VM even after the Job exits.
    if hours(timestamp(scope["cutoff"]) - now) > min(limits["hour_limit"], limits["dollar_limit_hours"]):
        raise ValueError("Full remaining VM allocation exceeds funding")


def check_projection(limits, projected_hours):
    if amount(projected_hours) > min(
        limits[k] for k in ("hour_limit", "dollar_limit_hours", "cutoff_limit_hours")
    ):
        raise ValueError(
            "Projected work exceeds the remaining hour, dollar or cutoff limit"
        )


def check_support(change, scope):
    if change["before"] != change["after"] or any(
        unknowns(change.get("after_unknown", {}))
    ):
        raise ValueError("Existing support worker must be unchanged")
    value = compact(change["after"])
    if (
        not scope.get("support_group_id")
        or value.pop("id") != scope["support_group_id"]
    ):
        raise ValueError("Existing support group does not match approved ID")
    status = value.pop("status")
    if (
        status.get("state") != "RUNNING"
        or status.get("reconciling")
        or status.get("ready_node_count") != 1
        or status.get("node_count") != 1
        or status.get("target_node_count") != 1
        or status.get("outdated_node_count", 0)
        or status.get("strategy") != value["strategy"]
        or not re.fullmatch(
            r"v?1\.36\.[0-9]+-nebius-node\.[0-9]+", status.get("version", "")
        )
    ):
        raise ValueError(
            "Support group is unready or its effective strategy/version differs"
        )
    for field in ("created_at", "updated_at", "metadata", "resource_version"):
        value.pop(field, None)
    template = value["template"]
    for field in ("local_disks", "gpu_settings"):
        if template.pop(field, None):
            raise ValueError("Unexpected devices on support worker")
    disk = template["boot_disk"]
    for unit, factor in (("bytes", 2**30), ("kibibytes", 2**20), ("mebibytes", 2**10)):
        if disk.pop("size_" + unit, 128 * factor) != 128 * factor:
            raise ValueError("Support disk size differs from 128 GiB")
    for field in ("block_size_bytes", "disk_encryption"):
        disk.pop(field, None)
    for nic in template["network_interfaces"]:
        if nic.get("security_groups") == []:
            del nic["security_groups"]
    check_group({"after": value}, "cpu", scope)


def check_stage(plan, scope, ledger, now):
    check_envelope(plan)
    stage = scope["stage"]
    mode = check_gpu_mode(scope, ledger)
    if plan["variables"].get("gpu_mode", {"value": "native"})["value"] != mode:
        raise ValueError("Saved plan GPU mode differs from the approved scope")
    if stage not in ("pilot", "support", "gpu"):
        raise ValueError("Expected pilot, support or gpu allocation stage")
    planned = timestamp(plan["timestamp"])
    if (
        not plan.get("applyable")
        or not timestamp(scope["start"]) <= planned <= now
        or now - planned > MAX_AGE
    ):
        raise ValueError("Saved plan is stale or inapplicable")
    for key in ("project_id", "subnet_id"):
        if plan["variables"][key]["value"] != scope[key]:
            raise ValueError(f"Wrong {key}")
    outputs = plan["planned_values"]["outputs"]
    if outputs["cluster_id"]["value"] != scope["cluster_id"] or (
        stage != "pilot" and outputs["pricing_policy_id"]["value"] != scope["policy_id"]
    ):
        raise ValueError("Wrong cluster or pricing policy")
    run = plan["variables"]["worker_run"]["value"]
    if (
        run["id"] != scope["run_id"]
        or any(run[k] != scope[k] for k in ("start", "cutoff"))
        or amount(run["other_cost_usd"]) != amount(scope["other_cost_usd"])
    ):
        raise ValueError("Wrong run reservation or window")
    if any(span["end"] is None for span in ledger["gpu_intervals"]):
        raise ValueError("Existing GPU allocation is still open")
    if stage == "pilot":
        limits = pilot_bounds(scope, ledger, now)
    else:
        limits = bounds(scope, ledger, now)
        # The GPU can bill until the watchdog fires, even when no Job runs.
        billed = hours(timestamp(scope["cutoff"]) - now)
        if billed > min(limits["hour_limit"], limits["dollar_limit_hours"]):
            raise ValueError(
                "Full window including cleanup exceeds the cumulative ledger"
            )
    expected = {CPU: ["create"]}
    if scope["stage"] == "gpu":
        expected = {CPU: ["no-op"], GPU: ["create"]}
    policy_checked = False
    for resource in plan["resource_changes"]:
        addr, change = resource["address"], resource["change"]
        actions = change["actions"]
        if resource.get("previous_address") or change.get("importing"):
            raise ValueError("Unexpected state migration")
        if addr in expected:
            if (
                actions != expected.pop(addr)
                or resource["type"] != "nebius_mk8s_v1_node_group"
            ):
                raise ValueError("Unexpected worker change: " + addr)
            if actions == ["no-op"]:
                check_support(change, scope)
            else:
                check_group(change, "gpu" if addr == GPU else "cpu", scope)
        elif actions != ["no-op"] or resource["type"] in (
            "nebius_mk8s_v1_node_group",
            "nebius_compute_v1_instance",
        ):
            raise ValueError("Unexpected resource: " + addr)
        elif (
            stage != "pilot" and resource["type"] == "nebius_billing_v1_pricing_policy"
        ):
            policy = change["after"]
            if (
                policy_checked
                or policy["id"] != scope["policy_id"]
                or policy["parent_id"] != scope["project_id"]
                or policy["compute_instance_spec"]["v1"]["platform"] != "gpu-rtx6000-a"
                or amount(policy["pricing"]["max_price_v1"]["max_price"]) != BID
                or policy["status"]["scheduling_state"] != "SCHEDULING_STATE_ALLOWED"
                or policy["status"]["currency"].lower() != "usd"
            ):
                raise ValueError("Expected the approved schedulable USD spot policy")
            policy_checked = True
    if expected or (stage != "pilot" and not policy_checked):
        raise ValueError("Missing required worker or pricing policy")
    return limits


EPOCH = 14084
EVALUATIONS = (*range(0, EPOCH, 500), EPOCH)
POST_WORK = {
    "confirmation_5163": 4,
    "calibration_scoring": 4,
    "language": 4,
    "clean_reload": 1,
}


def whole(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Count outside the selected fit protocol")
    return value


def backbone_of(request):
    return "flamingo" if "flamingo_binding" in request["pair"] else "qwen"


def fit_records(ledger, backbone):
    if backbone == "qwen":
        return ledger
    if backbone == "flamingo":
        return ledger.get("flamingo", {})
    raise ValueError("Unknown fit backbone")


def sha(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def terminal_complete(record, backbone):
    if record["terminal"] is not True:
        return False
    if not sha(record.get("export_sha256")):
        raise ValueError("Terminal arm needs a verified export")
    if backbone == "qwen":
        return True
    report = record.get("flamingo_qualification")
    pin = record.get("flamingo_qualification_sha256")
    if report is None and pin is None:
        return False  # direct-result.json may precede both fresh-process probes.
    if not isinstance(report, dict):
        raise ValueError("Missing verified Flamingo comparison")
    if hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest() != pin:
        raise ValueError("Flamingo comparison differs from its verified MLflow digest")
    for field in ("restored_receipts_sha256", "baseline_receipts_sha256"):
        pair = report.get(field)
        if not isinstance(pair, list) or len(pair) != 2 or not all(map(sha, pair)) or pair[0] == pair[1]:
            raise ValueError("Expected two independent receipts for " + field)
    if (report.get("selected_receipt_sha256") != record["export_sha256"]
            or not sha(report.get("selected_manifest_sha256"))
            or report.get("matching_horizons") != 6
            or any(type(report.get(k)) is not int or report[k] <= 0 for k in ("cases", "reference_tokens"))
            or any(type(report.get(k)) not in (int, float) for k in ("restored_max_abs_nll", "repeat_max_abs_nll"))
            or amount(report["restored_max_abs_nll"]) > amount(report["repeat_max_abs_nll"])):
        raise ValueError("Invalid verified Flamingo selection comparison")
    return True


def check_progress_sources(ledger, backbone, *, allow_legacy=False):
    selected = {
        (e["attempt_id"], str(e["request"]["seed"]), e["request"]["arm"]): e["request"]["pair"]
        for e in ledger.get("fit_attempts", [])
        if e["purpose"] == "selected" and backbone_of(e["request"]) == backbone
        and e.get("writer_fenced") is True and re.fullmatch("[0-9a-f]{64}", e.get("closeout_sha256") or "")
    }
    for seed, arms in fit_records(ledger, backbone).get("fit_progress", {}).items():
        for arm, record in arms.items():
            if allow_legacy and "attempt_id" not in record:
                continue
            source = (record.get("attempt_id"), seed, arm)
            if source not in selected:
                raise ValueError("Progress has no matching reconciled attempt")
            if backbone == "flamingo" and terminal_complete(record, backbone):
                if record["flamingo_qualification"]["baseline_receipts_sha256"] != selected[source]["flamingo_baselines"]:
                    raise ValueError("Selection comparison differs from the funded baselines")


def fit_projection(measurements, ledger, *, window_seconds=99 * 3600, after_preflight=False):
    """Price two ceiling-length arms; only the operator's verified ledger can credit work."""
    seed = measurements["seed"]
    if seed not in (27, 28) or not re.fullmatch(
        "[0-9a-f]{64}", measurements["measurement_sha256"]
    ):
        raise ValueError("Expected a selected seed and measured benchmark digest")
    every = whole(measurements["checkpoint_every"], 1, EPOCH)
    interval = whole(measurements.get("save_interval_seconds", 900), 0, 2**31 - 1)
    backbone = measurements.get("backbone", "qwen")
    progress = fit_records(ledger, backbone).get("fit_progress", {}).get(str(seed), {})
    flamingo = backbone == "flamingo"
    timing = {}
    save_key = "checkpoint_publish_seconds" if flamingo else "save_seconds"
    startup_key = "job_startup_restore_seconds" if flamingo else "startup_restore_seconds"
    for key in (
        "update_seconds",
        "fit_sample_pass_seconds",
        "validation_pass_seconds",
        save_key,
        startup_key,
        *(("allocated_preflight_seconds", "selected_restore_probe_seconds") if flamingo else ()),
        "export_seconds",
    ):
        observations = measurements["samples"][key]
        if not isinstance(observations, list) or not observations:
            raise ValueError(f"Missing timing observations for {key}")
        samples = [amount(x) for x in observations]
        if min(samples) <= 0:
            raise ValueError(f"Expected measured positive {key}")
        timing[key] = max(samples)
    if progress.keys() - {"control", "candidate"}:
        raise ValueError("Unexpected fit arm in the approved ledger")
    updates = evaluations = saves = starts = exports = 0
    replay_updates = replay_evals = 0
    eval_seconds = timing["fit_sample_pass_seconds"] + timing["validation_pass_seconds"]
    terminal_replay_extra = Decimal(0)
    save_retries = 0
    for arm in ("control", "candidate"):
        step, evaluated = 0, -1
        if arm in progress:
            record = progress[arm]
            if (
                not re.fullmatch("[0-9a-f]{64}", record["checkpoint_sha256"])
                or type(record["terminal"]) is not bool
            ):
                raise ValueError("Expected verified checkpoint and terminal status")
            step = whole(record["step"], 0, EPOCH)
            evaluated = whole(record["evaluated_through"], -1, step)
            if evaluated not in (-1, *EVALUATIONS):
                raise ValueError("Invalid completed evaluation step")
            if terminal_complete(record, backbone):
                continue
        remaining = EPOCH - step
        checks = sum(e > evaluated for e in EVALUATIONS)
        updates += remaining
        evaluations += checks
        # Price each periodic and best checkpoint and the final publication.
        saves += sum(e > step for e in EVALUATIONS) if flamingo else checks + 1
        starts += 1
        exports += 1
        training_replay = Decimal(0)
        if remaining:
            # One additional interruption up to the next durable save per arm.
            # Include setup, any evaluation in that stretch and the interrupted write.
            # The direct trainer always checkpoints at the 500-update evaluation grid.
            gap = 500 if flamingo else max(500, every)
            replay = min(1 if interval == 0 else gap, remaining)
            replay_updates += replay
            repeat_checks = sum(evaluated < e <= step + replay for e in EVALUATIONS)
            if flamingo:
                # An interruption may land at any future evaluation boundary.
                # Every scheduled evaluation publishes a checkpoint, so later
                # loss spans at most one evaluation. The initial span can also
                # contain the as-yet-unpublished diagnostic evaluation.
                repeat_checks = max(repeat_checks, int(checks > 0))
            replay_evals += repeat_checks
            save_retries += 1
            training_replay = replay * timing["update_seconds"] + repeat_checks * eval_seconds + timing[save_key]
        if flamingo:
            # Atomic restore receipts survive a retry. One interrupted receipt
            # can repeat under the same single-interruption allowance as training.
            # The worker also re-verifies and republishes terminal evidence.
            terminal_replay = timing["selected_restore_probe_seconds"] + timing["export_seconds"]
            terminal_replay_extra += max(Decimal(0), terminal_replay - training_replay)
        if remaining or flamingo:
            starts += 1
    # A wall-clock checkpoint resets the trainer timer after the save. Across
    # sequential workers their total running time cannot exceed the window.
    # At interval zero every completed update, including a replay, can save.
    updates_with_replay = updates + replay_updates
    timed_saves = updates_with_replay if not interval else min(
        updates_with_replay, int((amount(window_seconds) / interval).to_integral_value(rounding=ROUND_CEILING))
    )
    if flamingo:
        # At most one checkpoint per completed update. Replay ends at its
        # first (possibly interrupted) publication, already priced separately.
        timed_saves = min(timed_saves, updates)
        saves = min(updates, saves + timed_saves)
    else:
        saves += timed_saves
    saves += save_retries
    components = {
        "training": updates * timing["update_seconds"],
        "evaluations": evaluations * eval_seconds,
        "replay": replay_updates * timing["update_seconds"]
        + replay_evals * eval_seconds,
        "checkpoints": saves * timing[save_key],
        "startup_and_restore": starts * timing[startup_key],
        "exports": exports * timing["export_seconds"],
    }
    if flamingo:
        components.update(
            selected_restore_probes=2 * exports * timing["selected_restore_probe_seconds"],
            terminal_replay_extra=terminal_replay_extra,
            allocated_preflights=max(0, starts - int(after_preflight)) * timing["allocated_preflight_seconds"],
        )
    post_release = Decimal(0)
    if measurements["post_work"].keys() != POST_WORK.keys():
        raise ValueError(
            "Expected confirmation, calibration/scoring, language and clean restore"
        )
    for work, count in POST_WORK.items():
        item = measurements["post_work"][work]
        if flamingo and work in ("language", "clean_reload"):
            if item == {"when": "included-in-selected-restores"}:
                continue
            if item["when"] == "allocated" and item.get("scope") != "additional-package-tests":
                raise ValueError("Identify independent allocated Flamingo package tests or include them in selected restores")
        if item["when"] not in ("allocated", "after-release"):
            raise ValueError("Specify where post-fit work executes")
        seconds = count * amount(item["seconds_each"])
        if seconds <= 0:
            raise ValueError("Missing post-fit timing: " + work)
        if item["when"] == "allocated":
            components[work] = seconds
        else:
            rate = amount(item["hourly_usd"])
            post_release += seconds * rate / 3600
    if post_release > amount(ledger.get("post_release_usd", 0)):
        raise ValueError("Post-release work exceeds its funded ledger line")
    return {
        "seed": seed,
        "backbone": backbone,
        "updates": updates,
        "timed_checkpoint_upper_bound": timed_saves,
        "save_interval_seconds": interval,
        "evaluations": evaluations,
        "evaluation_window_visits": evaluations * 13328,
        "confirmation_window_visits": POST_WORK["confirmation_5163"] * 5163,
        "replay_updates": replay_updates,
        "post_release_usd": post_release,
        "component_seconds": components,
        "projected_hours": sum(components.values()) / 3600,
    }
