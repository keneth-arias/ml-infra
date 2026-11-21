from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "bootstrap/nebius"
guard = runpy.run_path(str(ROOT / "check-cpu-plan"))
expiry = runpy.run_path(str(ROOT / "expire-workers"))
now = datetime(2026, 11, 20, 10, 5, tzinfo=timezone.utc)
scope = dict(
    project_id="p",
    subnet_id="s",
    cluster_id="c",
    run_id="run",
    start="2026-11-20T10:00:00Z",
    cutoff="2026-11-20T18:00:00Z",
    accrued_gpu_hours=0,
    remaining_gpu_hours=100,
    accrued_gpu_usd=0,
    accrued_non_gpu_usd=2,
    cleanup_reserve_usd=2,
    other_remaining_usd=25,
    cpu_and_boot_disk_usd_per_hour=0.14444928,
)
after = dict(
    parent_id="c",
    version="1.36",
    fixed_node_count=1,
    name="training-cpu",
    labels={"airaware-run": "run", "airaware-cutoff": "20261120180000"},
    strategy={
        "max_surge": {"count": 0},
        "max_unavailable": {"count": 1},
        "drain_timeout": "5m",
    },
    template={
        "os": "ubuntu24.04",
        "resources": {"platform": "cpu-d3", "preset": "4vcpu-16gb"},
        "metadata": {"labels": {"airaware.training/pool": "training-cpu"}},
        "taints": [],
        "network_interfaces": [{"subnet_id": "s"}],
        "boot_disk": {"size_gibibytes": 128, "type": "NETWORK_SSD"},
    },
)
plan = dict(
    format_version="1.2",
    complete=True,
    errored=False,
    timestamp="2026-11-20T10:03:00Z",
    variables={
        "project_id": {"value": "p"},
        "subnet_id": {"value": "s"},
        "worker_run": {
            "value": {
                "start": scope["start"],
                "cutoff": scope["cutoff"],
                "id": "run",
                "other_cost_usd": 50,
            }
        },
    },
    planned_values={"outputs": {"cluster_id": {"value": "c"}}},
    resource_changes=[
        {
            "address": guard["CPU"],
            "type": "nebius_mk8s_v1_node_group",
            "change": {"actions": ["create"], "after": after},
        }
    ],
)


class CpuGuardTest(unittest.TestCase):
    def test_scoped_create_and_budget(self):
        guard["check"](plan, scope, now)
        for overrides in (
            {"cluster_id": "other"},
            {"run_id": "other"},
            {"remaining_gpu_hours": 101},
            {"other_remaining_usd": 75},
            {"accrued_gpu_usd": 101},
            {"cleanup_reserve_usd": 0},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                guard["check"](plan, {**scope, **overrides}, now)
        with self.assertRaises(ValueError):
            guard["check"](plan, scope, now.replace(hour=11))

    def test_changed_or_unknown_topology(self):
        for change in (
            {"after": {**after, "fixed_node_count": 2}},
            {"actions": ["delete", "create"]},
            {"after_unknown": {"template": {"resources": True}}},
            {
                "after": {
                    **after,
                    "template": {
                        **after["template"],
                        "network_interfaces": [
                            {"subnet_id": "s", "public_ip_address": {}}
                        ],
                    },
                }
            },
        ):
            p = deepcopy(plan)
            p["resource_changes"][0]["change"].update(change)
            with self.assertRaises(ValueError):
                guard["check"](p, scope, now)
        p = deepcopy(plan)
        p["resource_changes"].append(
            {
                "address": "policy",
                "type": "nebius_billing_v1_pricing_policy",
                "change": {"actions": ["update"]},
            }
        )
        with self.assertRaises(ValueError):
            guard["check"](p, scope, now)

    def test_inventory_after_state_loss_and_auth_failure(self):
        import subprocess

        baseline = dict(
            project_id="p", cluster_id="c", instance_ids=[], group_ids=[], disk_ids=[]
        )
        with patch(
            "subprocess.check_output",
            return_value=json.dumps({"items": [{"metadata": {"id": "orphan"}}]}),
        ):
            with self.assertRaises(ValueError):
                expiry["verify_inventory"]("profile", "p", "c", baseline)
        with patch(
            "subprocess.check_output", return_value='{"error":"inventory unavailable"}'
        ):
            with self.assertRaises(ValueError):
                expiry["verify_inventory"]("profile", "p", "c", baseline)
        with patch(
            "subprocess.check_output",
            side_effect=subprocess.CalledProcessError(1, ["nebius"]),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                expiry["verify_inventory"]("profile", "p", "c", baseline)
