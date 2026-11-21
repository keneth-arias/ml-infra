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

ROOT = Path(__file__).resolve().parents[1]
SKY = ROOT / "workloads/sky"


def inputs(mode="job", *opts):
    args = [
        "fit",
        "direct",
        "fixture",
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

    def get_object(self, **kwargs):
        data = self.raw
        if self.barrier:
            self.barrier.wait(timeout=5)
        return {"VersionId": "v1", "ETag": digest(data), "Body": BytesIO(data)}

    def put_object(self, *, IfMatch, Body, **kwargs):
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
        k: sc[k] for k in ("project_id", "cluster_id", "start", "cutoff", "other_cost_usd")
    }
    funds["fit_approval"].update(
        vm="worker", request=request, purpose="selected", max_attempts=2, measurements_sha256=digest(timing)
    )
    return sc, funds, timing


class FitAttemptTests(unittest.TestCase):
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

    def test_launcher_submits_only_the_reserved_bytes_after_successful_preflight(self):
        # The real renderer, launcher, funding guard and SDK call signatures run.
        # Stand-ins for the identity probe, two submitters and versioned storage
        # keep the native and SkyPilot paths entirely local.
        for mode in ("job", "sky"):
            args, request, task = inputs(mode)
            for outcome in ("approved", "preflight-failed", "budget-stop", "ambiguous"):
                with self.subTest(mode=mode, outcome=outcome), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)

                    def script(name, source):
                        path = root / name
                        path.write_text(source)
                        path.chmod(0o700)

                    script(
                        "python3",
                        f"""#!/bin/sh
case "$1" in */production_target.py) exit 0;; esac
exec "{sys.executable}" "$@"
""",
                    )
                    script(
                        "kubectl",
                        """#!/bin/bash
case " $* " in
  *" wait "*) [[ $OUTCOME != preflight-failed ]];;
  *" logs "*) exit 0;;
  *"pre.json"*) echo job/preflight;;
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
    return Storage()
""")
                    sc, funds, timing = fixture(request, datetime.now(timezone.utc))
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
                    if outcome == "approved":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(submitted.read_bytes(), task)
                        self.assertEqual(hold["request"]["task_sha256"], digest(submitted.read_bytes()))
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse(submitted.exists())
                        self.assertEqual(bool(hold), outcome == "ambiguous")
