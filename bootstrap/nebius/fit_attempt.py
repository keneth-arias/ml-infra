"""Bind a production paired fit to its render, remaining budget and off-host attempt."""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re

from control_storage import STORAGE_FIELDS, verify_bucket_projects
from gpu_reservation import read_ledger, with_ledger_client, write_reservation
from gpu_window import EPOCH, bounds, check_projection, fit_projection, whole
from worker_plan import amount, timestamp


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def describe(preflight, task):
    pod = json.loads(preflight)["spec"]["template"]["spec"]
    validator = pod["containers"][0]
    cmd = validator["command"]
    if "--direct-objective" not in cmd:
        return None

    def arg(name):
        return cmd[cmd.index("--" + name) + 1]

    spec = json.loads(task)
    if spec.get("kind") == "Job":
        mode = "job"
        pod_spec = spec["spec"]["template"]["spec"]
        deadline = spec["spec"]["activeDeadlineSeconds"]
    else:
        mode = "sky"
        pod_spec = spec["config"]["kubernetes"]["pod_config"]["spec"]
        deadline = pod_spec.get("activeDeadlineSeconds")
    grace = pod_spec.get("terminationGracePeriodSeconds")
    if grace != 90 or type(deadline) is not int or deadline <= 0:
        raise ValueError("Use cooperative shutdown and a bounded deadline for paid paired fits")
    arm = {"normalized-mse": "control", "physical-mae": "candidate"}[arg("direct-objective")]
    mounts = {v["mountPath"]: v["subPath"] for v in validator["volumeMounts"]}
    pair = {
        "fit": arg("bundle-sha256"),
        "validation": arg("validation-sha256"),
        "weights": mounts["/base"].split("/")[-1],
        "preflight_image": validator["image"],
        "trainer_image": next(v["value"] for v in validator["env"] if v["name"] == "EPISODE_IMAGE"),
    }
    return {
        "mode": mode,
        "task_sha256": digest(task),
        "preflight_sha256": digest(preflight),
        "run": arg("run-id"),
        "arm": arm,
        "seed": int(arg("seed")),
        "attempt": int(arg("attempt")),
        "ceiling": int(arg("step")),
        "save_interval_seconds": int(arg("save-seconds")),
        "pair": pair,
        "deadline_seconds": deadline + grace,
        "resume": "--resume" in cmd,
        "checkpoint": arg("resume-checkpoint") if "--resume-checkpoint" in cmd else None,
    }


def check_seed_28(ledger):
    pair = ledger["fit_progress"]["27"]
    gate = ledger["seed_28_decision"]
    for arm in ("control", "candidate"):
        progress = pair[arm]
        if (
            progress["terminal"] is not True
            or not sha(progress["export_sha256"])
            or gate["exports"][arm] != progress["export_sha256"]
        ):
            raise ValueError("Seed 28 requires both verified seed-27 exports")
    if (
        not sha(gate["evidence_sha256"])
        or type(gate["high_target_blocks"]) is not int
        or gate["high_target_blocks"] < 4
        or amount(gate["candidate_high_mae"]) >= amount(gate["control_high_mae"])
        or amount(gate["candidate_global_mae"]) > amount(gate["control_global_mae"])
    ):
        raise ValueError("Seed 27 does not qualify for replication")


def fit_attempt_id(purpose, request):
    ordinal = request["attempt"]
    if type(ordinal) is not int or ordinal < 1:
        raise ValueError("Invalid request ordinal for paired attempt ID")
    return f"paired-{purpose}-{request['seed']}-{request['arm']}-{ordinal}"


def previous_attempts(ledger, request, purpose, max_attempts):
    seed, arm, number = request["seed"], request["arm"], request["attempt"]
    history = ledger.get("fit_attempts", [])
    attempts = ledger["attempt_ids"]
    group = []
    seen = set()
    for entry in history:
        prev = entry["request"]
        if entry["attempt_id"] != fit_attempt_id(entry["purpose"], prev):
            raise ValueError("Stored paired attempt ID differs from its recorded request")
        if (
            entry["attempt_id"] in seen
            or entry["attempt_id"] not in attempts
            or not sha(entry.get("closeout_sha256"))
            or entry.get("writer_fenced") is not True
        ):
            raise ValueError("Reconcile the previous workload, writer and checkpoint before another launch")
        seen.add(entry["attempt_id"])
        same_arm = (entry["purpose"], prev["seed"], prev["arm"]) == (purpose, seed, arm)
        if (
            same_arm != (prev["run"] == request["run"])
            or entry["purpose"] == purpose
            and prev["pair"] != request["pair"]
        ):
            raise ValueError("Logical run or immutable paired inputs changed")
        if same_arm:
            if prev["attempt"] != len(group) + 1:
                raise ValueError("Paired attempt history is out of order")
            if prev["ceiling"] != request["ceiling"]:
                raise ValueError("Retry must retain the original training schedule")
            group.append(entry)
    if {a for a in attempts if a.startswith("paired-")} != seen:
        raise ValueError("Incomplete paired attempt history")
    attempt_id = fit_attempt_id(purpose, request)
    if (
        number != len(group) + 1
        or number > max_attempts
        or attempt_id in attempts
        or any(e["max_attempts"] != max_attempts for e in group)
    ):
        raise ValueError("Attempt already consumed, out of order or beyond the approved retry limit")

    return group, attempt_id


def recovery_checkpoint(ledger, approval, request, group):
    """Bind the one recovery drill to its completed control and replacement VM."""
    reference = approval["recovery_reference"]
    baseline = next((entry for entry in ledger["fit_attempts"]
                     if entry["attempt_id"] == "paired-qualification-27-control-1"), None)
    if (baseline is None or baseline["purpose"] != "qualification" or baseline["max_attempts"] != 1
            or baseline.get("outcome") != "complete"
            or reference != {"attempt_id": baseline["attempt_id"], "closeout_sha256": baseline["closeout_sha256"],
                             "checkpoint": baseline.get("qualification_checkpoint")}
            or any(baseline["request"][key] != request[key] for key in
                   ("mode", "seed", "arm", "pair", "ceiling", "save_interval_seconds"))
            or not re.fullmatch(rf"{request['ceiling']}:[0-9a-f]{{64}}", reference["checkpoint"] or "")):
        raise ValueError("Recovery reference must match the completed and verified control qualification")
    baseline_request = baseline["request"]
    if baseline_request["attempt"] != 1 or baseline_request["resume"] is not False or baseline_request["checkpoint"] is not None:
        raise ValueError("Recovery reference must be the fresh first control attempt")
    credit = baseline.get("selected_fit_credit")
    if type(credit) is not int or credit != 0:
        raise ValueError("Recovery reference must carry exactly zero selected fit credit")
    if not group:
        return None

    interrupted, = group
    checkpoint = interrupted.get("qualification_checkpoint") or ""
    if (interrupted.get("recovery_reference") != reference or interrupted.get("outcome") not in ("aborted", "preempted")
            or not re.fullmatch(r"[1-9][0-9]*:[0-9a-f]{64}", checkpoint)
            or int(checkpoint.split(":", 1)[0]) >= request["ceiling"]):
        raise ValueError("Recovery requires a verified interrupted checkpoint below the approved ceiling")
    if interrupted["vm"] == approval["vm"]:
        raise ValueError("Recovery must use a different GPU VM")
    current_vm, = [span for span in ledger["gpu_intervals"] if span["end"] is None]
    previous = [span for span in ledger["gpu_intervals"] if span["vm"] == interrupted["vm"]]
    if not previous or any(
        span["end"] is None or timestamp(span["end"]) > timestamp(current_vm["start"]) for span in previous
    ):
        raise ValueError("Reconcile and close the interrupted GPU allocation before opening its replacement")
    reserved = datetime.fromisoformat(interrupted["reserved_at"])
    if reserved.utcoffset() != timedelta(0) or not any(
        timestamp(span["start"]) <= reserved < timestamp(span["end"]) for span in previous
    ):
        raise ValueError("A closed interrupted GPU interval must cover its attempt reservation")
    return checkpoint


def reserve_fit(s3, scope, request, measurements, cluster, now):
    current, ledger = read_ledger(s3, scope)
    limits = bounds(scope, ledger, now)
    approval = ledger["fit_approval"]
    if (
        scope["stage"] != "gpu"
        or cluster != scope["cluster_id"]
        or any(
            approval.get(k) != scope[k]
            for k in ("cluster_id", "start", "cutoff", "other_cost_usd", *STORAGE_FIELDS)
        )
        or approval["request"] != request
        or approval["measurements_sha256"] != digest(measurements)
    ):
        raise ValueError("Workload, measurements or GPU window differ from the funded approval")
    open_vms = [v["vm"] for v in ledger["gpu_intervals"] if v["end"] is None]
    if open_vms != [approval["vm"]] or ledger.get("active_reservation"):
        raise ValueError("Require one approved live GPU and a reconciled previous reservation")
    seed, arm = request["seed"], request["arm"]
    purpose = approval["purpose"]
    if purpose == "selected":
        if request["ceiling"] != EPOCH:
            raise ValueError("Selected fit requires the full epoch ceiling")
    elif purpose not in ("qualification", "qualification-recovery") or seed != 27 or not 0 < request["ceiling"] < EPOCH:
        raise ValueError("Expected a selected pair or bounded seed-27 qualification")
    history = ledger.get("fit_attempts", [])
    max_attempts = whole(approval["max_attempts"], 1, 2**31 - 1)
    if purpose == "qualification-recovery" and (arm != "control" or request["mode"] != "job" or max_attempts != 2):
        raise ValueError("Recovery requires two separately approved native control qualification attempts")
    group, attempt_id = previous_attempts(ledger, request, purpose, max_attempts)
    projection = Decimal(request["deadline_seconds"]) / 3600
    checkpoint = None
    if purpose == "selected":
        timing = json.loads(measurements)
        if timing["seed"] != seed:
            raise ValueError("Wrong seed for fit projection")
        # Only earlier, reconciled attempts may earn credit towards either arm.
        for key, records in ledger.get("fit_progress", {}).items():
            for name, record in records.items():
                if not any(
                    e["purpose"] == "selected"
                    and str(e["request"]["seed"]) == key
                    and e["request"]["arm"] == name
                    and e["attempt_id"] == record["attempt_id"]
                    for e in history
                ):
                    raise ValueError("Progress has no matching reconciled attempt")
        if seed == 28:
            check_seed_28(ledger)
        progress = ledger.get("fit_progress", {}).get(str(seed), {}).get(arm)
        if progress:
            if progress["terminal"] or progress["step"] <= 0:
                raise ValueError("Selected arm already terminal or checkpoint has no completed update")
            checkpoint = f"{progress['step']}:{progress['checkpoint_sha256']}"
        seconds = (timestamp(scope["cutoff"]) - now).total_seconds()
        fit = fit_projection(timing, ledger, window_seconds=seconds)
        if request["save_interval_seconds"] != fit["save_interval_seconds"]:
            raise ValueError("Rendered save interval differs from the measured checkpoint policy")
        projection = fit["projected_hours"]
    elif purpose == "qualification-recovery":
        checkpoint = recovery_checkpoint(ledger, approval, request, group)
    elif group:
        checkpoint = group[-1].get("qualification_checkpoint")
        if checkpoint is not None and not re.fullmatch(r"[1-9][0-9]*:[a-f0-9]{64}", checkpoint):
            raise ValueError("Qualification closeout has an invalid recovery pin")
    if request["resume"] != bool(checkpoint) or request["checkpoint"] != checkpoint:
        raise ValueError("Resume must name the funded generation; start fresh with zero credited progress")
    if group and checkpoint is None and not sha(group[-1].get("fresh_state_sha256")):
        raise ValueError("Fresh replay requires fenced, verified empty run storage")
    check_projection(limits, projection)
    check_projection(limits, Decimal(request["deadline_seconds"]) / 3600)
    hold = dict(
        stage="fit",
        run_id=scope["run_id"],
        attempt_id=attempt_id,
        purpose=purpose,
        request=request,
        max_attempts=max_attempts,
        vm=approval["vm"],
        measurements_sha256=approval["measurements_sha256"],
        projected_hours=str(projection),
        limits={k: str(v) for k, v in limits.items()},
        prior_revision=ledger["revision"],
        reserved_at=now.astimezone(timezone.utc).isoformat(),
    )
    if purpose == "qualification-recovery":
        hold["recovery_reference"] = approval["recovery_reference"]
    ledger.setdefault("fit_attempts", []).append(hold)
    revision, version = write_reservation(s3, scope, current, ledger, hold)
    return {
        "attempt_id": attempt_id,
        "revision": revision,
        "version": version,
        "task_sha256": request["task_sha256"],
    }


def inspect_fresh(directory, attempt_id):
    # Run on the retained claim after the old writer has been fenced. Hidden
    # stage receipts and step directories are both significant for a fresh run.
    if directory.is_symlink() or not directory.is_dir() or any(directory.iterdir()):
        raise ValueError("Expected an empty, prepared state directory for fresh replay")
    return {"after_attempt": attempt_id, "state_path": str(directory.resolve()), "empty": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    fresh = sub.add_parser("check-fresh", help="Attest an empty previously fenced replay directory")
    fresh.add_argument("directory", type=Path)
    fresh.add_argument("--after-attempt", required=True)
    for action in ("describe", "reserve"):
        command = sub.add_parser(action)
        command.add_argument("preflight", type=Path)
        command.add_argument("task", type=Path)
    args = parser.parse_args()
    if args.action == "check-fresh":
        print(json.dumps(inspect_fresh(args.directory, args.after_attempt), sort_keys=True))
        return
    request = describe(args.preflight.read_bytes(), args.task.read_bytes())
    if request is None:
        return
    if args.action == "describe":
        print(json.dumps(request, sort_keys=True))
        return
    scope = json.loads(Path(os.environ["NEBIUS_FIT_SCOPE"]).read_bytes())
    verify_bucket_projects(scope)
    timing = Path(os.environ["NEBIUS_FIT_MEASUREMENTS"]).read_bytes()
    print(
        json.dumps(
            with_ledger_client(
                reserve_fit, scope, request, timing, os.environ["NEBIUS_TRAINING_CLUSTER_ID"]
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SystemExit(f"Fit reservation refused: {exc}")
