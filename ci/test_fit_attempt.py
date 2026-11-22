"""Exercise real renders against a versioned, conditional ledger and both launch paths."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier, Lock
import unittest

from test_gpu_plan import NOW, ledger, scope, timings, utc
from fit_attempt import check_seed_28, describe, digest, reserve_fit
from gpu_window import fit_projection

ROOT = Path(__file__).resolve().parents[1]
SKY = ROOT / "workloads/sky"


def inputs(mode="job", *opts, run_id="fixture"):
    args = [
        "fit",
        "direct",
        run_id,
        "--image",
        "ghcr.io/test/t@sha256:" + "a" * 64,
        "--preflight-image",
        "ghcr.io/test/p@sha256:" + "b" * 64,
        "--bundle-sha256",
        "c" * 64,
        "--validation-sha256",
        "d" * 64,
        "--cached-weights-sha256",
        "e" * 64,
        "--step",
        "14084",
        "--schedule",
        "14084",
        "--direct-objective",
        "normalized-mse",
        "--graceful-stop",
        *opts,
    ]

    def render(kind):
        return subprocess.check_output(
            [sys.executable, SKY / "episode-profile.py", kind, *args, "--target", "prod-nebius"]
        )

    preflight, task = render("preflight"), render(mode)
    return args, describe(preflight, task), task


class Storage:
    def __init__(self, ledger, barrier=None):
        self.raw = json.dumps(ledger).encode()
        self.barrier = barrier
        self.lock = Lock()

    def get_object(self, *, Bucket, Key):
        assert (Bucket, Key) == ("controls", "prod/mk8s/release-control/ledger.json")
        data = self.raw
        if self.barrier:
            self.barrier.wait(timeout=5)
        return {"VersionId": "v1", "ETag": digest(data), "Body": BytesIO(data)}

    def put_object(self, *, Bucket, Key, IfMatch, Body, **kwargs):
        assert (Bucket, Key) == ("controls", "prod/mk8s/release-control/ledger.json")
        with self.lock:
            if IfMatch != digest(self.raw):
                raise ValueError("Conditional write conflict")
            self.raw = Body
            return {"VersionId": digest(self.raw)}


def fixture(request, now=NOW):
    sc = dict(
        scope(),
        stage="gpu",
        start=utc(now - timedelta(hours=1)),
        cutoff=utc(now + timedelta(hours=10)),
        state_bucket="fixture",
        control_bucket="controls",
        control_project_id="management-project",
    )
    timing = json.dumps(dict(timings(), seed=request["seed"])).encode()
    funds = dict(
        ledger(),
        observed_at=utc(now),
        attempt_ids=[],
        active_reservation=None,
        gpu_intervals=[{"vm": "worker", "start": sc["start"], "end": None}],
    )
    funds["fit_approval"] = {
        k: sc[k] for k in ("project_id", "cluster_id", "start", "cutoff", "other_cost_usd",
                                  "state_bucket", "control_bucket", "control_project_id")
    }
    funds["fit_approval"].update(
        vm="worker", request=request, purpose="selected", max_attempts=2, measurements_sha256=digest(timing)
    )
    return sc, funds, timing


class FitAttemptTests(unittest.TestCase):
    def test_storage_scope_is_required_before_any_reservation_io(self):
        from unittest.mock import Mock

        _, request, _ = inputs()
        approved, _, timing = fixture(request)
        for value in (None, approved["state_bucket"]):
            with self.subTest(value=value):
                sc = dict(approved)
                if value is None:
                    sc.pop("control_bucket")
                else:
                    sc["control_bucket"] = value
                s3 = Mock()
                with self.assertRaisesRegex(ValueError, "control_bucket"):
                    reserve_fit(s3, sc, request, timing, "c", NOW)
                self.assertEqual(s3.mock_calls, [])

    def test_two_runners_and_ambiguous_delivery(self):
        _, req, _ = inputs()
        sc, funds, timing = fixture(req)
        s3 = Storage(funds, Barrier(2))
        sc["ledger_sha256"] = digest(s3.raw)
        with ThreadPoolExecutor(2) as pool:
            results = [pool.submit(reserve_fit, s3, sc, req, timing, "c", NOW) for _ in range(2)]
        self.assertEqual(sum(r.exception() is None for r in results), 1)
        funds = json.loads(s3.raw)
        s3 = Storage(funds)
        sc["ledger_sha256"] = digest(s3.raw)
        with self.assertRaisesRegex(ValueError, "previous reservation"):
            reserve_fit(s3, sc, req, timing, "c", NOW)
        self.assertEqual(len(funds["attempt_ids"]), 1)

    def test_recovery_requires_evidence_and_the_pinned_generation(self):
        _, req, _ = inputs()
        sc, funds, timing = fixture(req)

        def consume(request):
            s3 = Storage(funds)
            sc["ledger_sha256"] = digest(s3.raw)
            reserve_fit(s3, sc, request, timing, "c", NOW)
            return json.loads(s3.raw)

        funds = consume(req)
        entry = funds["fit_attempts"][0]
        funds["active_reservation"] = None
        checkpoint = "500:" + "f" * 64
        _, retry, _ = inputs("sky", "--attempt", "2", "--resume", "--resume-checkpoint", checkpoint)
        funds["fit_approval"]["request"] = retry
        with self.assertRaisesRegex(ValueError, "writer"):
            consume(retry)
        entry.update(writer_fenced=True, closeout_sha256="f" * 64)
        with self.assertRaisesRegex(ValueError, "funded generation"):
            consume(retry)
        record = {
            "step": 500,
            "evaluated_through": 500,
            "checkpoint_sha256": "f" * 64,
            "terminal": False,
            "attempt_id": entry["attempt_id"],
        }
        funds["fit_progress"] = {"27": {"control": record}}
        with self.subTest("foreign-progress"):
            record["attempt_id"] = "unknown"
            with self.assertRaisesRegex(ValueError, "matching reconciled"):
                consume(retry)
            record["attempt_id"] = entry["attempt_id"]
        result = consume(retry)
        self.assertEqual(result["active_reservation"]["request"]["checkpoint"], checkpoint)
        funds = result
        funds["active_reservation"] = None
        funds["fit_attempts"][-1].update(writer_fenced=True, closeout_sha256="a" * 64)
        third = dict(retry, attempt=3)
        funds["fit_approval"]["request"] = third
        with self.assertRaisesRegex(ValueError, "retry limit"):
            consume(third)

    def test_budget_and_scope_denials_leave_the_ledger_intact(self):
        _, request, _ = inputs()
        for case in (
            "closed",
            "unversioned",
            "hours",
            "tax",
            "cutoff",
            "different-render",
            "target",
            "short",
            "replication",
        ):
            with self.subTest(case=case):
                req = deepcopy(request)
                if case == "replication":
                    req["seed"] = 28
                if case == "short":
                    req["ceiling"] = 500
                sc, funds, timing = fixture(req)
                if case == "hours":
                    funds["gpu_intervals"].append(
                        {"vm": "old", "start": utc(NOW - timedelta(hours=99)), "end": utc(NOW)}
                    )
                if case == "tax":
                    funds["tax_remaining_usd"] = 100
                if case == "cutoff":
                    funds["fit_approval"]["cutoff"] = sc["cutoff"] = utc(NOW + timedelta(hours=2))
                if case == "closed":
                    funds["gpu_intervals"][0]["end"] = utc(NOW)
                if case == "different-render":
                    req = dict(req, task_sha256="0" * 64)
                s3 = Storage(funds)
                if case == "unversioned":
                    get = s3.get_object
                    s3.get_object = lambda **kw: dict(get(**kw), VersionId="null")
                sc["ledger_sha256"] = digest(s3.raw)
                with self.assertRaises((ValueError, KeyError)):
                    reserve_fit(s3, sc, req, timing, "other" if case == "target" else "c", NOW)
                self.assertEqual(digest(s3.raw), sc["ledger_sha256"])

    def test_rendered_save_cadence_is_funded_for_the_full_pair(self):
        _, req, _ = inputs("job", "--save-seconds", "0")
        sc, funds, timing = fixture(req)

        def consume():
            s3 = Storage(funds)
            sc["ledger_sha256"] = digest(s3.raw)
            reserve_fit(s3, sc, req, timing, "c", NOW)

        with self.assertRaisesRegex(ValueError, "save interval"):
            consume()
        observations = dict(timings(), save_interval_seconds=0)
        price = fit_projection(observations, funds)
        self.assertGreaterEqual(price["timed_checkpoint_upper_bound"], 28168)
        timing = json.dumps(observations).encode()
        funds["fit_approval"]["measurements_sha256"] = digest(timing)
        with self.assertRaisesRegex(ValueError, "Projected work exceeds"):
            consume()

    def test_qualification_resume_preserves_checkpoint_schedule(self):
        _, req, _ = inputs("job", "--step", "500", "--schedule", "500")
        sc, funds, timing = fixture(req)
        funds["fit_approval"]["purpose"] = "qualification"
        s3 = Storage(funds)
        sc["ledger_sha256"] = digest(s3.raw)
        reserve_fit(s3, sc, req, timing, "c", NOW)
        funds = json.loads(s3.raw)
        pin = "300:" + "f" * 64
        funds["active_reservation"] = None
        funds["fit_attempts"][0].update(
            closeout_sha256="a" * 64, writer_fenced=True, qualification_checkpoint=pin
        )
        for ceiling in (501, 500):
            _, retry, _ = inputs(
                "sky",
                "--step",
                str(ceiling),
                "--schedule",
                str(ceiling),
                "--attempt",
                "2",
                "--resume",
                "--resume-checkpoint",
                pin,
            )
            funds["fit_approval"]["request"] = retry
            s3 = Storage(funds)
            sc["ledger_sha256"] = digest(s3.raw)
            if ceiling == 501:
                with self.assertRaisesRegex(ValueError, "training schedule"):
                    reserve_fit(s3, sc, retry, timing, "c", NOW)
            else:
                reserve_fit(s3, sc, retry, timing, "c", NOW)

    def test_bounded_recovery_uses_a_closed_control_and_a_different_vm(self):
        _, control, _ = inputs("job", "--step", "500", "--schedule", "500")
        _, first, _ = inputs("job", "--step", "500", "--schedule", "500", run_id="recovery-drill")
        first_at = NOW - timedelta(minutes=10)
        sc, funds, timing = fixture(first, first_at)
        baseline = {
            "attempt_id": "paired-qualification-27-control-1", "purpose": "qualification",
            "request": control, "max_attempts": 1, "vm": "worker",
            "outcome": "complete", "writer_fenced": True, "selected_fit_credit": 0,
            "closeout_sha256": "f" * 64, "qualification_checkpoint": "500:" + "a" * 64,
        }
        funds["fit_attempts"] = [baseline]
        funds["attempt_ids"] = [baseline["attempt_id"]]
        reference = {key: baseline[key] for key in ("attempt_id", "closeout_sha256")}
        reference["checkpoint"] = baseline["qualification_checkpoint"]
        funds["fit_approval"].update(purpose="qualification-recovery", recovery_reference=reference)
        for attempt_patch, message in (
            ({"request": {**control, "attempt": 7}}, "attempt ID"),
            ({"request": {**control, "resume": True}}, "fresh"),
            ({"request": {**control, "checkpoint": "200:" + "a" * 64}}, "fresh"),
            ({"selected_fit_credit": False}, "zero selected"),
            ({"selected_fit_credit": 0.0}, "zero selected"),
        ):
            with self.subTest(attempt_patch=attempt_patch):
                modified = {**funds, "fit_attempts": [{**baseline, **attempt_patch}]}
                store = Storage(modified)
                with self.assertRaisesRegex(ValueError, message):
                    reserve_fit(store, {**sc, "ledger_sha256": digest(store.raw)}, first, timing, "c", first_at)
        s3 = Storage(funds)
        sc["ledger_sha256"] = digest(s3.raw)
        first_receipt = reserve_fit(s3, sc, first, timing, "c", first_at)
        self.assertEqual(first_receipt["attempt_id"], "paired-qualification-recovery-27-control-1")
        closed = json.loads(s3.raw)
        self.assertEqual(closed["fit_attempts"][0], baseline)
        self.assertEqual(closed["active_reservation"]["recovery_reference"], reference)
        pin = "300:" + "b" * 64
        closed["fit_attempts"][-1].update(closeout_sha256="c" * 64, writer_fenced=True,
                                        outcome="aborted", qualification_checkpoint=pin)
        closed["active_reservation"] = None
        _, retry, _ = inputs("job", "--step", "500", "--schedule", "500", "--attempt", "2",
                            "--resume", "--resume-checkpoint", pin, run_id="recovery-drill")
        closed["fit_approval"]["request"] = retry

        def consume(funds, request=retry):
            store = Storage(funds)
            scoped = {**sc, "ledger_sha256": digest(store.raw)}
            return reserve_fit(store, scoped, request, timing, "c", NOW)

        with self.assertRaisesRegex(ValueError, "different GPU"):
            consume(closed)
        closed["gpu_intervals"][0]["end"] = utc(NOW - timedelta(minutes=2))
        closed["gpu_intervals"].append({"vm": "replacement", "start": utc(NOW - timedelta(minutes=1)), "end": None})
        closed["fit_approval"]["vm"] = "replacement"
        bad = deepcopy(closed)
        bad["fit_attempts"][-1]["qualification_checkpoint"] = "500:" + "b" * 64
        with self.assertRaisesRegex(ValueError, "interrupted checkpoint"):
            consume(bad)
        bad = deepcopy(closed)
        bad["fit_approval"]["recovery_reference"]["closeout_sha256"] = "e" * 64
        with self.assertRaisesRegex(ValueError, "reference"):
            consume(bad)
        bad = deepcopy(closed)
        bad["fit_approval"]["max_attempts"] = 3
        with self.assertRaisesRegex(ValueError, "two separately approved"):
            consume(bad)
        bad = deepcopy(closed)
        bad["gpu_intervals"][0]["end"] = utc(NOW)
        with self.assertRaisesRegex(ValueError, "close the interrupted GPU allocation"):
            consume(bad)
        bad = deepcopy(closed)
        bad["gpu_intervals"][0]["end"] = utc(first_at - timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "interval must cover"):
            consume(bad)
        bad = deepcopy(closed)
        bad["gpu_intervals"][0]["start"] = utc(first_at + timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "interval must cover"):
            consume(bad)
        bad = deepcopy(closed)
        bad["gpu_intervals"][0]["end"] = None
        with self.assertRaisesRegex(ValueError, "one approved live GPU"):
            consume(bad)
        bad = deepcopy(closed)
        bad["fit_attempts"][-1]["request"]["attempt"] = 7
        with self.assertRaisesRegex(ValueError, "attempt ID"):
            consume(bad)
        self.assertEqual(consume(closed)["attempt_id"], "paired-qualification-recovery-27-control-2")

    def test_seed_28_uses_both_completed_exports_and_the_preregistered_scores(self):
        exports = {"control": "c" * 64, "candidate": "d" * 64}
        gate = dict(
            exports=exports,
            evidence_sha256="e" * 64,
            high_target_blocks=4,
            control_high_mae=3,
            candidate_high_mae=2,
            control_global_mae=1,
            candidate_global_mae=1,
        )
        funds = {
            "seed_28_decision": gate,
            "fit_progress": {"27": {k: dict(terminal=True, export_sha256=v) for k, v in exports.items()}},
        }
        check_seed_28(funds)
        for patch in (
            dict(high_target_blocks=3),
            dict(candidate_high_mae=3),
            dict(candidate_global_mae=2),
            dict(exports=dict(exports, candidate="0" * 64)),
        ):
            with self.assertRaises(ValueError):
                check_seed_28(dict(funds, seed_28_decision={**gate, **patch}))

    def test_backbones_share_funding_and_fences_but_have_independent_fit_state(self):
        _, qwen, _ = inputs(run_id="qwen")
        _, flamingo, _ = inputs("job", "--flamingo-binding-sha256", "1" * 64,
                                "--flamingo-baselines", "2" * 64, "3" * 64,
                                "--flamingo-parity-sha256", "4" * 64, run_id="flamingo")
        requests = {"qwen": qwen, "flamingo": flamingo}
        for order in (("qwen", "flamingo"), ("flamingo", "qwen")):
            with self.subTest(order=order):
                sc, funds, _ = fixture(requests[order[0]])

                def consume(family, purpose, req, **approval):
                    # Leave Qwen measurement files and ledger records in their legacy format.
                    obs = dict(timings(), seed=req["seed"])
                    if family == "flamingo":
                        obs["backbone"] = family
                    timing = json.dumps(obs).encode()
                    funds["fit_approval"].update(purpose=purpose, request=req, max_attempts=1,
                                                 measurements_sha256=digest(timing))
                    funds["fit_approval"].update(approval)
                    store = Storage(funds)
                    scoped = dict(sc, ledger_sha256=digest(store.raw))
                    receipt = reserve_fit(store, scoped, req, timing, "c", NOW)
                    funds.update(json.loads(store.raw))
                    entry = funds["fit_attempts"][-1]
                    self.assertEqual(entry["attempt_id"], receipt["attempt_id"])
                    prefix = "paired-flamingo" if family == "flamingo" else "paired"
                    self.assertEqual(entry["attempt_id"],
                                     f"{prefix}-{purpose}-{req['seed']}-{req['arm']}-{req['attempt']}")
                    funds["active_reservation"] = None
                    entry.update(writer_fenced=True, closeout_sha256="a" * 64)
                    return entry

                refs = {}
                for family in order:
                    base = dict(requests[family], run=family + "-qual", ceiling=500)
                    entry = consume(family, "qualification", base)
                    pin = "500:" + "e" * 64
                    entry.update(outcome="complete", qualification_checkpoint=pin, selected_fit_credit=0)
                    refs[family] = dict(attempt_id=entry["attempt_id"],
                                        closeout_sha256=entry["closeout_sha256"], checkpoint=pin)
                for family in order:
                    drill = dict(requests[family], run=family + "-drill", ceiling=500)
                    other = next(f for f in order if f != family)
                    with self.assertRaisesRegex(ValueError, "reference"):
                        consume(family, "qualification-recovery", drill, max_attempts=2,
                                recovery_reference=refs[other])
                    consume(family, "qualification-recovery", drill, max_attempts=2,
                            recovery_reference=refs[family])

                first, second = order
                for arm, pin in (("control", "b" * 64), ("candidate", "c" * 64)):
                    req = dict(requests[first], arm=arm, run=first + "-" + arm)
                    if arm == "candidate":
                        changed = dict(req, pair={**req["pair"], "weights": "9" * 64})
                        with self.assertRaisesRegex(ValueError, "immutable"):
                            consume(first, "selected", changed)
                    entry = consume(first, "selected", req)
                    obs = dict(timings(), backbone=first)
                    projection = fit_projection(obs, funds, window_seconds=36000)
                    self.assertEqual(projection["updates"], 14084 if arm == "candidate" else 28168)
                    self.assertEqual(entry["projected_hours"], str(projection["projected_hours"]))
                    record = dict(terminal=True, step=14084, evaluated_through=14084,
                                  checkpoint_sha256=pin, export_sha256=pin, attempt_id=entry["attempt_id"])
                    # consume reloads the ledger after each successful reservation.
                    namespace = funds if first == "qwen" else funds.setdefault("flamingo", {})
                    namespace.setdefault("fit_progress", {}).setdefault("27", {})[arm] = record
                namespace["seed_28_decision"] = dict(exports={"control": "b" * 64, "candidate": "c" * 64},
                    evidence_sha256="e" * 64, high_target_blocks=4, control_high_mae=3,
                    candidate_high_mae=2, control_global_mae=1, candidate_global_mae=1)
                consume(first, "selected", dict(requests[first], seed=28, run=first + "-replication"))

                req = requests[second]
                with self.assertRaisesRegex(ValueError, "Wrong backbone"):
                    consume(first, "selected", req)
                with self.assertRaisesRegex(ValueError, "Logical run"):
                    consume(second, "selected", dict(req, run=first + "-control"))
                with self.assertRaises((ValueError, KeyError)):
                    consume(second, "selected", dict(req, seed=28, run=second + "-replication"))
                with self.assertRaisesRegex(ValueError, "funded generation"):
                    consume(second, "selected", dict(req, resume=True, checkpoint="14084:" + "b" * 64))
                funds["fit_attempts"][0]["writer_fenced"] = False
                with self.assertRaisesRegex(ValueError, "writer"):
                    consume(second, "selected", req)
                funds["fit_attempts"][0]["writer_fenced"] = True
                funds["accrued_gpu_usd"] = 150
                with self.assertRaises(ValueError):
                    consume(second, "selected", req)
                funds["accrued_gpu_usd"] = 0
                entry = consume(second, "selected", req, max_attempts=2)
                obs = dict(timings(), backbone=second)
                expected = fit_projection(obs, {}, window_seconds=36000)
                self.assertEqual(entry["projected_hours"], str(expected["projected_hours"]))
                namespace = funds if second == "qwen" else funds.setdefault("flamingo", {})
                record = dict(terminal=False, step=500, evaluated_through=500, checkpoint_sha256="f" * 64,
                              attempt_id=f"paired-{'flamingo-' if first == 'flamingo' else ''}selected-27-control-1")
                namespace["fit_progress"] = {"27": {"control": record}}
                retry = dict(req, attempt=2, resume=True, checkpoint="500:" + "f" * 64)
                with self.assertRaisesRegex(ValueError, "matching reconciled"):
                    consume(second, "selected", retry, max_attempts=2)
                record["attempt_id"] = entry["attempt_id"]
                consume(second, "selected", retry, max_attempts=2)

    def test_launcher_submits_only_the_reserved_bytes_after_successful_preflight(self):
        # The real renderer, launcher, funding guard and SDK call signatures run.
        # Stand-ins for the identity and release checks, submitters and storage
        # keep the native and SkyPilot paths entirely local.
        for mode in ("job", "sky", "preflight"):
            for outcome in (
                "approved",
                "early-release-denied",
                "late-release-denied",
                "release-promoted",
                "preflight-failed",
                "handoff-failed",
                "budget-stop",
                "wrong-bucket-owner",
                "ambiguous",
                "dirty-retry",
                "fresh-retry",
            ):
                if mode == "preflight" and outcome not in ("approved", "early-release-denied", "preflight-failed"):
                    continue
                retry = outcome.endswith("-retry")
                args, request, task = inputs("job" if mode == "preflight" else mode, *(("--attempt", "2") if retry else ()))
                with self.subTest(mode=mode, outcome=outcome), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)

                    def script(name, source):
                        path = root / name
                        path.write_text(source)
                        path.chmod(0o700)

                    script(
                        "python3",
                        f"""#!/bin/sh
set -eu
case "$1" in
  */production_target.py) exit 0;;
  */production_release.py)
    [ "$#" -eq 4 ] && [ "$2" = check-episode ] || exit 2
    stage=early
    if [ -f "$FIXTURE/preflight" ]; then stage=late; fi
    cp "$3" "$FIXTURE/$stage-preflight"
    cp "$4" "$FIXTURE/$stage-task"
    [ "$OUTCOME" != "$stage-release-denied" ] || exit 1
    if [ "$OUTCOME" = release-promoted ] && [ "$stage" = late ]; then
      echo "{'d' * 64}"
    else
      echo "{'c' * 64}"
    fi
    exit 0;;
  */training-storage-handoff.py)
    if [ -f "$FIXTURE/preflight" ] && [ "$OUTCOME" = handoff-failed ]; then exit 1; fi
    exit 0;;
  */fit_attempt.py) touch "$FIXTURE/funding-guard";;
esac
exec "{sys.executable}" "$@"
""",
                    )
                    script(
                        "kubectl",
                        """#!/bin/bash
set -eu
case " $* " in
  *" wait "*) [[ $OUTCOME != preflight-failed ]];;
  *" logs "*) exit 0;;
  *"pre.json"*) while [[ $1 != -f ]]; do shift; done
    cp "$2" "$FIXTURE/preflight" && echo job/preflight;;
  *"task.yaml"*) cp "${!#}" "$FIXTURE/submitted";;
  *) exit 1;;
esac
""",
                    )
                    script(
                        "sky",
                        """#!/bin/bash
case "$1" in
  status) echo '[]';;
  launch) cp "${!#}" "$FIXTURE/submitted";;
  *) exit 1;;
esac
""",
                    )
                    script(
                        "nebius",
                        f"""#!{sys.executable}
import json
import os
import sys
name = sys.argv[sys.argv.index("--name") + 1]
project = sys.argv[sys.argv.index("--parent-id") + 1]
assert project == {{"fixture": "p", "controls": "management-project"}}[name]
if name == "controls" and os.environ["OUTCOME"] == "wrong-bucket-owner":
    project = "another-project"
print(json.dumps({{"metadata": {{"name": name, "parent_id": project}}}}))
""",
                    )
                    (root / "boto3.py").write_text("""
import os
from io import BytesIO
from pathlib import Path
class Storage:
    path = Path(os.environ["FIXTURE"]) / "ledger.json"
    def get_object(self, **kw):
        return dict(ETag='"old"', VersionId='v1', Body=BytesIO(self.path.read_bytes()))
    def put_object(self, *, IfMatch, Body, **kw):
        assert IfMatch == '"old"'
        self.path.write_bytes(Body)
        return dict(VersionId="null" if os.environ["OUTCOME"] == "ambiguous" else "v2")
def client(*a, **kw):
    (Path(os.environ["FIXTURE"]) / "s3-access").touch()
    return Storage()
""")
                    sc, funds, timing = fixture(request, datetime.now(timezone.utc))
                    if retry:
                        _, original, _ = inputs(mode)
                        now = datetime.now(timezone.utc)
                        sc, funds, timing = fixture(original, now)
                        storage = Storage(funds)
                        sc["ledger_sha256"] = digest(storage.raw)
                        receipt = reserve_fit(storage, sc, original, timing, "c", now)
                        funds = json.loads(storage.raw)
                        funds.update(active_reservation=None)
                        previous = funds["fit_attempts"][0]
                        previous.update(closeout_sha256="b" * 64, writer_fenced=True)
                        state = root / "state"
                        (state / "step-00000500").mkdir(parents=True)
                        check = [
                            sys.executable,
                            ROOT / "bootstrap/nebius/fit_attempt.py",
                            "check-fresh",
                            state,
                            "--after-attempt",
                            receipt["attempt_id"],
                        ]
                        self.assertNotEqual(subprocess.run(check, capture_output=True).returncode, 0)
                        if outcome == "fresh-retry":
                            state.rename(root / "quarantined")
                            state.mkdir()
                            previous["fresh_state_sha256"] = digest(subprocess.check_output(check))
                        funds["fit_approval"]["request"] = request
                    if outcome == "budget-stop":
                        funds["tax_remaining_usd"] = 100
                    raw = json.dumps(funds).encode()
                    (root / "ledger.json").write_bytes(raw)
                    sc["ledger_sha256"] = digest(raw)
                    (root / "scope").write_text(json.dumps(sc))
                    (root / "timing").write_bytes(timing)
                    env = dict(
                        os.environ,
                        PATH=f"{root}:{os.environ['PATH']}",
                        PYTHONPATH=str(root),
                        NEBIUS_SUBMIT_KUBECONFIG=str(root / "unused"),
                        NEBIUS_TRAINING_CLUSTER_ID="c",
                        NEBIUS_FIT_SCOPE=str(root / "scope"),
                        NEBIUS_FIT_MEASUREMENTS=str(root / "timing"),
                        FIXTURE=str(root),
                        OUTCOME=outcome,
                    )
                    result = subprocess.run(
                        [SKY / "launch-episode.sh", "--nebius", mode, *args], env=env, capture_output=True
                    )
                    submitted = root / "submitted"
                    hold = json.loads((root / "ledger.json").read_bytes())["active_reservation"]
                    if mode == "preflight":
                        self.assertEqual(result.returncode == 0, outcome == "approved", result.stderr)
                        self.assertEqual((root / "preflight").exists(), outcome != "early-release-denied")
                        self.assertTrue((root / "early-preflight").is_file())
                        self.assertFalse((root / "funding-guard").exists())
                        self.assertFalse((root / "s3-access").exists())
                        self.assertFalse(submitted.exists())
                        continue
                    for stage in ("early", "late"):
                        if stage == "late" and outcome in ("early-release-denied", "preflight-failed", "handoff-failed"):
                            self.assertFalse((root / "late-task").exists())
                            continue
                        self.assertEqual((root / f"{stage}-task").read_bytes(), task)
                        checked_preflight = (root / f"{stage}-preflight").read_bytes()
                        self.assertEqual(digest(checked_preflight), request["preflight_sha256"])
                    if outcome == "early-release-denied":
                        self.assertFalse((root / "preflight").exists())
                    else:
                        self.assertEqual(digest((root / "preflight").read_bytes()), request["preflight_sha256"])
                    if outcome in ("early-release-denied", "late-release-denied", "release-promoted", "handoff-failed"):
                        self.assertFalse((root / "funding-guard").exists())
                        self.assertFalse((root / "s3-access").exists())
                        self.assertEqual((root / "ledger.json").read_bytes(), raw)
                    if outcome in ("approved", "fresh-retry"):
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(submitted.read_bytes(), task)
                        self.assertEqual(hold["request"]["task_sha256"], digest(submitted.read_bytes()))
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(submitted.exists())
                        self.assertEqual(bool(hold), outcome == "ambiguous")
                        if outcome == "wrong-bucket-owner":
                            self.assertIn(b"Unexpected owning project", result.stderr)
                            self.assertFalse((root / "s3-access").exists())
                            self.assertEqual((root / "ledger.json").read_bytes(), raw)
