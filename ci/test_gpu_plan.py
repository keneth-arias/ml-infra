from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap/nebius"))
import gpu_window

NOW = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)


def utc(t):
    return t.isoformat().replace("+00:00", "Z")


def ledger():
    return dict(
        revision="r8",
        observed_at=utc(NOW),
        run_id="fit",
        gpu_intervals=[],
        accrued_gpu_usd="0",
        accrued_non_gpu_usd="2",
        other_remaining_usd="8",
        tax_remaining_usd="2",
        cleanup_other_usd="1",
        cpu_and_boot_hourly_usd="0.15",
        gpu_boot_hourly_usd="0.02",
        drain_hours="0.25",
    )


def scope():
    return dict(
        run_id="fit",
        project_id="p",
        state_bucket="state",
        control_bucket="controls",
        control_project_id="management-project",
        subnet_id="s",
        cluster_id="c",
        policy_id="policy",
        start=utc(NOW - timedelta(hours=1)),
        cutoff=utc(NOW + timedelta(hours=10)),
        other_cost_usd=100,
    )


class BudgetTests(unittest.TestCase):
    def test_all_three_bounds_and_exact_fit(self):
        funds = ledger()
        funds["gpu_intervals"] = [
            {"vm": "first", "start": utc(NOW - timedelta(hours=89)), "end": utc(NOW)}
        ]
        funds["accrued_gpu_usd"] = "133.5"
        limits = gpu_window.bounds(scope(), funds, NOW)
        self.assertEqual(limits["hour_limit"], 10)
        self.assertEqual(limits["dollar_limit_hours"], 10)
        self.assertEqual(limits["cutoff_limit_hours"], Decimal("9.75"))
        gpu_window.check_projection(limits, Decimal("9.75"))
        with self.assertRaises(ValueError):
            gpu_window.check_projection(limits, Decimal("9.76"))

    def test_union_per_vm_and_charge_open_intervals(self):
        a, b = utc(NOW - timedelta(hours=3)), utc(NOW - timedelta(hours=1))
        items = [
            {"vm": "a", "start": a, "end": b},
            {"vm": "a", "start": b, "end": None},
            {"vm": "a", "start": a, "end": b},
            {"vm": "b", "start": b, "end": None},
        ]
        funds = dict(
            ledger(), gpu_intervals=items, observed_at=utc(NOW - timedelta(minutes=10))
        )
        limits = gpu_window.bounds(scope(), funds, NOW)
        self.assertEqual(limits["gpu_hours_used"], 4)
        self.assertEqual(
            limits["dollar_limit_hours"], Decimal("98.66666666666666666666666667")
        )

    def test_stale_or_unfunded_ledger(self):
        for updates in (
            dict(observed_at=utc(NOW - timedelta(minutes=16))),
            dict(observed_at=utc(NOW + timedelta(seconds=1))),
            dict(tax_remaining_usd=100),
            dict(accrued_gpu_usd=150),
            dict(run_id="another"),
            dict(gpu_boot_hourly_usd=0),
            dict(
                gpu_intervals=[
                    dict(vm="vm", start=utc(NOW - timedelta(hours=100)), end=None)
                ]
            ),
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                gpu_window.bounds(scope(), dict(ledger(), **updates), NOW)


def group(pool):
    gpu = pool == "gpu"
    labels = {"airaware.training/pool": "training-" + pool}
    template = {
        "metadata": {"labels": labels},
        "network_interfaces": [{"subnet_id": "s"}],
        "os": "ubuntu24.04",
        "taints": [],
        "resources": {"platform": "cpu-d3", "preset": "4vcpu-16gb"},
        "boot_disk": {"size_gibibytes": 128, "type": "NETWORK_SSD"},
    }
    if gpu:
        labels["skypilot.co/accelerator"] = "rtxpro6000"
        template.update(
            resources={"platform": "gpu-rtx6000-a", "preset": "1gpu-24vcpu-218gb"},
            preemptible={},
            spot_pricing_policy={"id": "policy"},
            gpu_settings={"drivers_preset": "cuda13.0"},
            taints=[
                {"effect": "NO_SCHEDULE", "key": "nvidia.com/gpu", "value": "true"}
            ],
        )
    return {
        "address": f'nebius_mk8s_v1_node_group.training["{pool}"]',
        "mode": "managed",
        "type": "nebius_mk8s_v1_node_group",
        "change": {
            "actions": ["create"],
            "before": None,
            "after": {
                "parent_id": "c",
                "name": "training-" + pool,
                "fixed_node_count": 1,
                "version": "1.36",
                "labels": {"airaware-run": "fit", "airaware-cutoff": "20261121200000"},
                "strategy": {
                    "max_surge": {"count": 0},
                    "max_unavailable": {"count": 1},
                    "drain_timeout": "5m",
                },
                "template": template,
            },
        },
    }


def preview(stage):
    sc = scope()
    cpu = group("cpu")
    if stage == "pilot":
        sc["cutoff"] = utc(NOW + timedelta(hours=7))  # start was an hour ago
        cpu["change"]["after"]["labels"]["airaware-cutoff"] = "20261121170000"
    resources = [cpu]
    if stage == "gpu":
        sc["support_group_id"] = "the-support"
        value = cpu["change"]["after"]
        value.update(
            id="the-support",
            created_at="earlier",
            status={
                "state": "RUNNING",
                "node_count": 1,
                "target_node_count": 1,
                "ready_node_count": 1,
                "strategy": value["strategy"],
                "version": "1.36.3-nebius-node.80",
            },
        )
        cpu["change"].update(before=deepcopy(value), actions=["no-op"])
        resources.append(group("gpu"))
    resources.append(
        {
            "address": "nebius_billing_v1_pricing_policy.training[0]",
            "mode": "managed",
            "type": "nebius_billing_v1_pricing_policy",
            "change": {
                "actions": ["no-op"],
                "after": {
                    "id": "policy",
                    "parent_id": "p",
                    "compute_instance_spec": {"v1": {"platform": "gpu-rtx6000-a"}},
                    "pricing": {"max_price_v1": {"max_price": "1.50"}},
                    "status": {
                        "scheduling_state": "SCHEDULING_STATE_ALLOWED",
                        "currency": "usd",
                    },
                },
            },
        }
    )
    sc["stage"] = stage
    return sc, {
        "format_version": "1.2",
        "applyable": True,
        "complete": True,
        "errored": False,
        "timestamp": utc(NOW),
        "planned_values": {
            "outputs": {
                "cluster_id": {"value": "c"},
                "pricing_policy_id": {"value": "policy"},
            }
        },
        "variables": {
            "project_id": {"value": "p"},
            "subnet_id": {"value": "s"},
            "worker_run": {
                "value": dict(
                    id="fit", start=sc["start"], cutoff=sc["cutoff"], other_cost_usd=100
                )
            },
        },
        "resource_changes": resources,
    }


class TopologyTests(unittest.TestCase):
    def test_pilot_prices_only_cpu_and_allows_an_unchanged_blocked_spot_policy(self):
        sc, plan = preview("pilot")
        funds = dict(ledger(), remaining_gpu_hours=100)
        del funds["gpu_boot_hourly_usd"]
        del sc["policy_id"]
        policy = plan["resource_changes"][-1]["change"]
        policy["after"]["status"]["scheduling_state"] = "BLOCKED"
        limits = gpu_window.check_stage(plan, sc, funds, NOW)
        self.assertEqual(limits["cpu_and_boot_window_usd"], Decimal("1.35"))
        self.assertEqual(limits["funded_total_usd"], 250)
        policy["actions"] = ["update"]
        with self.assertRaises(ValueError):
            gpu_window.check_stage(plan, sc, funds, NOW)

    def test_pilot_rejects_gpu_spend_hours_unfunded_costs_and_extra_workers(self):
        sc, plan = preview("pilot")
        funds = dict(ledger(), remaining_gpu_hours=100)
        for changes in (
            {"accrued_gpu_usd": "0.01"},
            {"remaining_gpu_hours": "100.01"},
            {"tax_remaining_usd": "98"},
            {"observed_at": utc(NOW - timedelta(minutes=16))},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gpu_window.check_stage(plan, sc, dict(funds, **changes), NOW)
        with self.assertRaises(ValueError):
            gpu_window.pilot_bounds(
                dict(sc, start=utc(NOW - timedelta(hours=2))), funds, NOW
            )
        plan["resource_changes"].append(group("gpu"))
        with self.assertRaises(ValueError):
            gpu_window.check_stage(plan, sc, funds, NOW)

    def test_both_stages(self):
        for stage in ("support", "gpu"):
            sc, plan = preview(stage)
            gpu_window.check_stage(plan, sc, ledger(), NOW)

    def test_policy_extra_node_and_stale_plan(self):
        for case in (
            "policy",
            "extra",
            "stale",
            "unready",
            "wrong-id",
            "duplicate",
            "drift",
            "budget",
        ):
            sc, p = preview("gpu")
            rs = p["resource_changes"]
            funds = ledger()
            if case == "policy":
                rs[-1]["change"]["after"]["status"]["scheduling_state"] = "BLOCKED"
            elif case == "extra":
                rs[0] = group("cpu")
            elif case == "stale":
                p["timestamp"] = utc(NOW - timedelta(minutes=16))
            elif case == "unready":
                for state in ("before", "after"):
                    rs[0]["change"][state]["status"]["ready_node_count"] = 0
            elif case == "wrong-id":
                sc["support_group_id"] = "other"
            elif case == "duplicate":
                rs.append(group("gpu"))
            elif case == "drift":
                p["resource_drift"] = [
                    dict(address="unexpected", change=dict(actions=["update"]))
                ]
            elif case == "budget":
                funds["accrued_gpu_usd"] = 140
            with self.subTest(case=case), self.assertRaises(ValueError):
                gpu_window.check_stage(p, sc, funds, NOW)

    def test_gpu_inputs_and_support_only(self):
        for path, value in (
            (("fixed_node_count",), 2),
            (("template", "resources", "preset"), "other"),
            (("template", "resources", "platform"), "other"),
            (("template", "spot_pricing_policy", "id"), "other"),
            (("template", "preemptible"), None),
            (("template", "taints"), []),
            (
                ("template", "network_interfaces"),
                [{"subnet_id": "s", "public_ip_address": {}}],
            ),
            (("template", "gpu_settings", "drivers_preset"), "other"),
        ):
            sc, p = preview("gpu")
            field = p["resource_changes"][1]["change"]["after"]
            for key in path[:-1]:
                field = field[key]
            field[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                gpu_window.check_stage(p, sc, ledger(), NOW)
        sc, p = preview("support")
        p["resource_changes"].append(group("gpu"))
        with self.assertRaises(ValueError):
            gpu_window.check_stage(p, sc, ledger(), NOW)


class SavedPlanTests(unittest.TestCase):
    def test_receipt_binds_the_approved_inputs_and_plan_bytes(self):
        import hashlib
        import runpy
        from tempfile import TemporaryDirectory

        command = runpy.run_path(
            str(Path(gpu_window.__file__).with_name("check-gpu-plan"))
        )
        with TemporaryDirectory() as tmp:
            plan = Path(tmp) / "saved.tfplan"
            plan.write_bytes(b"plan")
            scope_digest = hashlib.sha256(b"scope").hexdigest()
            ledger_digest = hashlib.sha256(b"ledger").hexdigest()
            inputs = {
                "scope_sha256": scope_digest,
                "ledger_sha256": ledger_digest,
                "plan_sha256": hashlib.sha256(b"plan").hexdigest(),
            }
            command["verify_digests"](plan, scope_digest, ledger_digest, inputs)
            for name in inputs:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    command["verify_digests"](
                        plan, scope_digest, ledger_digest, {**inputs, name: "0" * 64}
                    )
            plan.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                command["verify_digests"](plan, scope_digest, ledger_digest, inputs)


def timings():
    return {
        "seed": 27,
        "checkpoint_every": 500,
        "measurement_sha256": "a" * 64,
        "samples": {
            k: ["0.1", "1"]
            for k in (
                "update_seconds",
                "fit_sample_pass_seconds",
                "validation_pass_seconds",
                "save_seconds",
                "startup_restore_seconds",
                "export_seconds",
            )
        },
        "post_work": {
            k: {"when": "allocated", "seconds_each": 1}
            for k in (
                "confirmation_5163",
                "calibration_scoring",
                "language",
                "clean_reload",
            )
        },
    }


class FitProjectionTests(unittest.TestCase):
    def test_epoch_evaluations_replay_and_post_work(self):
        result = gpu_window.fit_projection(timings(), ledger())
        self.assertEqual(result["updates"], 28168)
        self.assertEqual(result["evaluations"], 60)
        self.assertEqual(result["evaluation_window_visits"], 799680)
        self.assertEqual(result["confirmation_window_visits"], 20652)
        self.assertEqual(result["replay_updates"], 1000)
        self.assertGreater(result["projected_hours"], Decimal(28168) / 3600)
        limits = gpu_window.bounds(scope(), ledger(), NOW)
        short = {**limits, "cutoff_limit_hours": 2}
        gpu_window.check_projection(short, 1)
        with self.assertRaises(ValueError):
            gpu_window.check_projection(short, result["projected_hours"])

    def test_retry_credits_only_verified_progress_from_the_selected_seed(self):
        funds = dict(
            ledger(),
            fit_progress={
                "27": {
                    "control": {
                        "step": 1000,
                        "evaluated_through": 1000,
                        "terminal": False,
                        "checkpoint_sha256": "a" * 64,
                    }
                }
            },
        )
        self.assertEqual(gpu_window.fit_projection(timings(), funds)["updates"], 27168)
        self.assertEqual(
            gpu_window.fit_projection({**timings(), "seed": 28}, funds)["updates"],
            28168,
        )
        del funds["fit_progress"]["27"]["control"]["checkpoint_sha256"]
        with self.assertRaises((KeyError, ValueError)):
            gpu_window.fit_projection(timings(), funds)

    def test_work_after_drain_requires_separate_funding(self):
        obs = timings()
        obs["post_work"]["calibration_scoring"] = {
            "when": "after-release",
            "seconds_each": 3600,
            "hourly_usd": 2,
        }
        with self.assertRaises(ValueError):
            gpu_window.fit_projection(obs, ledger())
        self.assertEqual(
            gpu_window.fit_projection(obs, dict(ledger(), post_release_usd=8))[
                "post_release_usd"
            ],
            8,
        )


class ProjectionCommandTest(unittest.TestCase):
    def test_cli_publishes_a_budget_stop_or_a_measured_fit(self):
        import hashlib
        import json
        import subprocess
        from tempfile import TemporaryDirectory

        now = datetime.now(timezone.utc)
        inputs = dict(
            scope(),
            start=utc(now - timedelta(minutes=1)),
            cutoff=utc(now + timedelta(hours=9)),
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            observations = root / "measurements.json"
            observations.write_text(json.dumps(timings()))
            command = [
                sys.executable,
                str(Path(gpu_window.__file__).with_name("check-gpu-plan")),
                "project",
                "--scope",
                str(root / "scope.json"),
                "--ledger",
                str(root / "ledger.json"),
                "--measurements",
                str(observations),
            ]
            for spent, decision in (
                (0, "fits"),
                (147, "budget-stop"),
                (150, "budget-stop"),
            ):
                raw = json.dumps(
                    dict(ledger(), observed_at=utc(now), accrued_gpu_usd=spent)
                ).encode()
                (root / "ledger.json").write_bytes(raw)
                (root / "scope.json").write_text(
                    json.dumps(
                        {**inputs, "ledger_sha256": hashlib.sha256(raw).hexdigest()}
                    )
                )
                receipt = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(
                    receipt.returncode, 0 if decision == "fits" else 2, receipt.stderr
                )
                data = json.loads(receipt.stdout)
                self.assertEqual(data["decision"], decision)
                self.assertIn("dollar_limit_hours", data["bounds"])
                self.assertEqual(data["fit"]["updates"], 28168)

            for bad_scope in (
                {k: v for k, v in inputs.items() if k != "control_bucket"},
                dict(inputs, control_bucket=inputs["state_bucket"]),
            ):
                bad_scope["ledger_sha256"] = hashlib.sha256(raw).hexdigest()
                (root / "scope.json").write_text(json.dumps(bad_scope))
                receipt = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(receipt.returncode, 1)
                self.assertIn("control_bucket", receipt.stderr)


class ReservationTests(unittest.TestCase):
    def test_atomic_consumption_requires_current_bytes_and_two_valid_hosts(self):
        import hashlib
        import io
        import json
        from unittest.mock import Mock
        from gpu_reservation import reserve, reserve_remote
        from worker_cleanup import cleanup_digest, scope_digest

        root = Path(gpu_window.__file__).parent
        units = {
            name: root / "management" / name
            for name in (
                "worker-expiry@.service",
                "worker-expiry@.timer",
                "worker-attest@.service",
            )
        }
        source = cleanup_digest(root, units)

        sc = dict(
            scope(),
            stage="gpu",
            attempt_id="attempt-1",
            state_bucket="state",
            state_lineage="lineage",
            baseline_sha256="b" * 64,
            cleanup_accounts={"bm": "primary", "fw13": "backup"},
            fw13_cronjob={"uid": "cron-uid", "spec_sha256": "c" * 64,
                          "image": "local/backup@sha256:" + "1" * 64},
            fw13_public_key_id="fw13-key",
            fw13_tfvars_sha256="d" * 64,
            fw13_attest_until=utc(NOW + timedelta(minutes=3)),
        )
        grants = {
            host: dict(
                account_id=account,
                public_key_id=host + "-key",
                state_bucket="state",
                state_key="prod/mk8s/terraform.tfstate",
                control_bucket="controls",
                control_project_id=sc["control_project_id"],
                timer_prefix=f"prod/mk8s/release-control/timers/{host}/",
                timer_write=True,
                project_id="p",
                cluster_id="c",
                evidence_sha256="d" * 64,
                checked_at=utc(NOW),
                valid_through=utc(NOW + timedelta(hours=9)),
                node_group_delete=True,
                state_write=True,
                lock_write_delete=True,
            )
            for host, account in sc["cleanup_accounts"].items()
        }
        raw = json.dumps(
            dict(ledger(), remaining_gpu_hours=100, cleanup_access=grants)
        ).encode()
        sc["ledger_sha256"] = hashlib.sha256(raw).hexdigest()

        def run(case):
            s3 = Mock()
            s3.put_object.return_value = {"VersionId": "null" if case == "unversioned-write" else "new-version"}

            def read(**req):
                self.assertEqual(req["Bucket"], "controls")
                if req["Key"].endswith("/ledger.json"):
                    return {
                        "Body": io.BytesIO(raw if case != "stale" else b"{}"),
                        "ETag": '"current"',
                        "VersionId": "null" if case == "unversioned-ledger" else "funded-version",
                    }
                host = req["Key"].split("/")[-3]
                self.assertEqual(req["Key"], f"prod/mk8s/release-control/timers/{host}/fit/{sc['stage']}.json")
                data = dict(
                    host=host,
                    stage=sc["stage"],
                    run_id="fit",
                    cutoff=sc["cutoff"],
                    project_id="p",
                    cluster_id="c",
                    state_lineage="lineage",
                    state_bucket="state",
                    control_bucket="controls" if case != "wrong-control-proof" else "another",
                    control_project_id=sc["control_project_id"],
                    state_serial=3,
                    baseline_sha256="b" * 64,
                    timer="worker-expiry@fit.timer",
                    observed_at=utc(NOW),
                    next_tick=utc(NOW + timedelta(seconds=30)),
                    enabled=True,
                    active=True,
                    cloud_state_access=True,
                    scope_sha256=scope_digest(sc),
                    cleanup_source_sha256=source,
                    inventory_reconciled=True,
                    locked_release_plan_checked=True,
                    account_id=sc["cleanup_accounts"][host],
                    provider_key_id=host + "-key",
                )
                if host == "fw13":
                    data.update(timer="cronjob/expiry-fit", observed_at=utc(NOW), next_tick=utc(NOW + timedelta(minutes=1)),
                                kind_cronjob=dict(sc["fw13_cronjob"], scheduled_at=utc(NOW), pod_uid="pod", job_uid="job"))
                    if case == "old-host-timer":
                        data["timer"] = "worker-expiry@fit.timer"
                    if case == "swapped-cronjob":
                        data["kind_cronjob"]["uid"] = "another"
                if case == "timer" and host == "fw13":
                    data["run_id"] = "old"
                if case == "old-code":
                    data.pop("cleanup_source_sha256")
                if case == "unrehearsed":
                    data["locked_release_plan_checked"] = False
                if case == "wrong-stage":
                    data["stage"] = "support"
                if case == "expired":
                    data["next_tick"] = utc(NOW - timedelta(seconds=1))
                return {
                    "Body": io.BytesIO(json.dumps(data).encode()),
                    "VersionId": "null" if case == "unversioned" and host == "fw13" else "evidence",
                }

            s3.get_object.side_effect = read
            if case == "conflict":
                from botocore.exceptions import ClientError
                from unittest.mock import patch

                s3.put_object.side_effect = ClientError(
                    {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}},
                    "PutObject",
                )
                with patch("boto3.client", return_value=s3), patch("gpu_reservation.datetime") as clock:
                    clock.now.return_value = NOW
                    with self.assertRaisesRegex(ValueError, "inspect the off-host ledger") as caught:
                        reserve_remote(sc, "a" * 64, 3)
                self.assertIsInstance(caught.exception.__cause__, ClientError)
                self.assertEqual(s3.put_object.call_count, 1)
                self.assertEqual(s3.put_object.call_args.kwargs["IfMatch"], '"current"')
            elif case in ("missing-control", "same-control"):
                bad_scope = dict(sc)
                if case == "missing-control":
                    bad_scope.pop("control_bucket")
                else:
                    bad_scope["control_bucket"] = bad_scope["state_bucket"]
                with self.assertRaisesRegex(ValueError, "control_bucket"):
                    reserve(s3, bad_scope, "a" * 64, 3, NOW)
                s3.get_object.assert_not_called()
                s3.put_object.assert_not_called()
            elif case == "invalid-cleanup-account":
                bad_scope = dict(sc, cleanup_accounts={"bm": ["primary"], "fw13": "backup"})
                with self.assertRaisesRegex(ValueError, "distinct identities"):
                    reserve(s3, bad_scope, "a" * 64, 3, NOW)
                s3.put_object.assert_not_called()
            elif case == "unversioned-write":
                with self.assertRaisesRegex(ValueError, "inspect the off-host ledger"):
                    reserve(s3, sc, "a" * 64, 3, NOW)
                s3.put_object.assert_called_once()
            elif case in (
                "unversioned-ledger",
                "unversioned",
                "held",
                "consumed",
                "timer",
                "expired",
                "stale",
                "old-host-timer",
                "swapped-cronjob",
                "wrong-stage",
                "wrong-control-proof",
                "old-code",
                "unrehearsed",
            ):
                expected = {
                    "unversioned-ledger": "Funding ledger requires a versioned object",
                    "unversioned": "Timer attestation requires a versioned object",
                    "held": "previously consumed reservation",
                    "consumed": "previously consumed reservation",
                }.get(case, ".")
                with self.assertRaisesRegex(ValueError, expected):
                    reserve(s3, sc, "a" * 64, 3, NOW)
                s3.put_object.assert_not_called()
            else:
                reserve(s3, sc, "a" * 64, 3, NOW)
                entry = s3.put_object.call_args.kwargs
                self.assertEqual(entry["Bucket"], "controls")
                self.assertEqual(entry["IfMatch"], '"current"')
                hold = json.loads(entry["Body"])["active_reservation"]
                self.assertEqual(hold["plan_sha256"], "a" * 64)
                self.assertEqual(set(hold["timer_versions"]), {"bm", "fw13"})
                if sc["stage"] == "pilot":
                    self.assertEqual(Decimal(hold["gpu_hours"]), 0)
                    self.assertEqual(Decimal(hold["gpu_usd"]), 0)
                    self.assertEqual(Decimal(hold["non_gpu_outstanding_usd"]), 98)

        for stage in ("gpu", "pilot"):
            sc.update(stage=stage, cutoff=utc(NOW + timedelta(hours=7)))
            for case in (
                "ok",
                "invalid-cleanup-account",
                "missing-control",
                "same-control",
                "unversioned-ledger",
                "unversioned-write",
                "conflict",
                "unversioned",
                "timer",
                "expired",
                "stale",
                "old-host-timer",
                "swapped-cronjob",
                "wrong-stage",
                "wrong-control-proof",
                "old-code",
                "unrehearsed",
            ):
                with self.subTest(stage=stage, case=case):
                    run(case)

        for case, previous in (
            ("held", {"active_reservation": {"attempt_id": "previous"}}),
            ("consumed", {"attempt_ids": [sc["attempt_id"]]}),
        ):
            raw = json.dumps(dict(ledger(), remaining_gpu_hours=100, cleanup_access=grants, **previous)).encode()
            sc["ledger_sha256"] = hashlib.sha256(raw).hexdigest()
            with self.subTest(case=case):
                run(case)

        # The live ledger carries evidence of provider deletion authority separately
        # from the cleanup process that can only demonstrate a read-only rehearsal.
        for key, value in (
            ("node_group_delete", False),
            ("valid_through", utc(NOW)),
            ("state_bucket", "other"),
            ("state_key", "other"),
            ("control_bucket", "other"),
            ("timer_prefix", "other"),
            ("timer_write", False),
        ):
            bad = deepcopy(grants)
            bad["fw13"][key] = value
            raw = json.dumps(
                dict(ledger(), remaining_gpu_hours=100, cleanup_access=bad)
            ).encode()
            sc["ledger_sha256"] = hashlib.sha256(raw).hexdigest()
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(ValueError, "cleanup grant evidence"),
            ):
                run("ok")


class ApplyConsumptionTests(unittest.TestCase):
    def test_only_reserved_exact_bytes_reach_terraform_apply(self):
        import argparse
        import hashlib
        import json
        import runpy
        import subprocess
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock, patch
        from zipfile import ZipFile

        root = Path(gpu_window.__file__).parent
        check = runpy.run_path(str(root / "check-gpu-plan"))["checked_plan"]
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            saved = d / "test.tfplan"
            with ZipFile(saved, "w") as z:
                for name in ("main.tf", "versions.tf"):
                    z.write(root / name, "tfconfig/m-/" + name)
                z.write(root / ".terraform.lock.hcl", ".terraform.lock.hcl")
                z.writestr("tfconfig/modules.json", "{}")
                z.writestr("tfstate", json.dumps(dict(lineage="test", serial=3)))
            funds = ledger()
            sc, plan = preview("support")

            def sha(path):
                return hashlib.sha256(path.read_bytes()).hexdigest()

            (d / "ledger").write_text(json.dumps(funds))
            sc.update(
                ledger_sha256=sha(d / "ledger"),
                state_bucket="state",
                state_lineage="test",
                attempt_id="a",
            )
            (d / "scope").write_text(json.dumps(sc))
            (d / "receipt").write_text(
                json.dumps(
                    dict(
                        plan_sha256=sha(saved),
                        ledger_sha256=sha(d / "ledger"),
                        scope_sha256=sha(d / "scope"),
                    )
                )
            )
            applied = []

            def bucket_record(argv, **kwargs):
                name = argv[argv.index("--name") + 1]
                project = argv[argv.index("--parent-id") + 1]
                self.assertEqual({"state": "p", "controls": "management-project"}[name], project)
                return json.dumps({"metadata": {"name": name, "parent_id": project}}).encode()

            def terraform(argv, **kwargs):
                phase = argv[1]
                if phase == "state":
                    out = json.dumps(dict(lineage="test", serial=3))
                elif phase == "show":
                    out = json.dumps(plan)
                elif phase == "init":
                    self.assertIn("-backend-config=bucket=state", argv)
                    out = ""
                elif phase == "apply":
                    self.assertNotEqual(Path(argv[-1]), saved)
                    applied.append(sha(Path(argv[-1])))
                    out = ""
                else:
                    self.fail(argv)
                return subprocess.CompletedProcess(argv, 0, stdout=out.encode())

            args = argparse.Namespace(
                command="apply",
                plan=saved,
                ledger=d / "ledger",
                scope=d / "scope",
                receipt=d / "receipt",
            )

            class Clock:
                @staticmethod
                def now(tz):
                    return NOW

            with (
                patch("subprocess.run", side_effect=terraform),
                patch("subprocess.check_output", side_effect=bucket_record) as lookup,
                patch.dict(check.__globals__, datetime=Clock),
            ):
                with patch.dict(
                    check.__globals__,
                    reserve_remote=Mock(side_effect=ValueError("unfunded")),
                ):
                    with self.assertRaises(ValueError):
                        check(args)
                self.assertEqual(applied, [])
                with patch.dict(
                    check.__globals__,
                    reserve_remote=lambda *args: ("revision-2", "version-2"),
                ):
                    result = check(args)
                self.assertEqual(applied, [result["plan_sha256"]])
                self.assertEqual(result["control_bucket"], "controls")
                self.assertEqual(lookup.call_count, 4)

                lookup.side_effect = None
                lookup.return_value = b'{"metadata": {"name": "state", "parent_id": "another"}}'
                with self.assertRaisesRegex(ValueError, "Unexpected owning project"):
                    check(args)
                self.assertEqual(applied, [result["plan_sha256"]])
