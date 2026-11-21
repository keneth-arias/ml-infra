"""Verify managed inventory and plan a locked release of this run’s workers."""

import hashlib
import json
import os
import subprocess


GROUPS = {
    'nebius_mk8s_v1_node_group.training["cpu"]',
    'nebius_mk8s_v1_node_group.training["gpu"]',
}


def release_candidates(plan, cluster_id, run_id, cutoff_label, *, target_ids=None):
    if (
        plan.get("format_version", "").split(".")[0] != "1"
        or (target_ids is None and plan.get("complete") is not True)
        or not isinstance(plan.get("complete"), bool)
        or plan.get("errored") is not False
        or plan.get("deferred_changes")
        or plan.get("action_invocations")
    ):
        raise ValueError("Incomplete or unsupported release plan.")
    releases = []
    planned_ids = []
    for resource in plan.get("resource_changes", []):
        address = resource["address"]
        change = resource["change"]
        actions = change["actions"]
        before = change.get("before") or {}
        labels = before.get("labels") or {}
        owned = (
            address in GROUPS
            and before.get("parent_id") == cluster_id
            and labels.get("airaware-run") == run_id
            and labels.get("airaware-cutoff") == cutoff_label
        )
        if actions == ["no-op"] and not owned:
            continue
        if resource["mode"] == "data" and actions == ["read"]:
            continue
        if actions != ["delete"] or not owned:
            raise ValueError(f"Unexpected change at cutoff: {address} {actions}")
        releases.append(address)
        if target_ids is not None:
            planned_ids.append(before["id"])
    if target_ids is not None and (
        len(set(planned_ids)) != len(planned_ids)
        or set(planned_ids) != target_ids
        or (planned_ids and plan.get("applyable") is not True)
    ):
        raise ValueError("Release plan differs from live run-owned groups")
    return releases


def cleanup_digest(root, units):
    paths = {
        name: root / name
        for name in (
            "expire-workers",
            "worker_cleanup.py",
            "with-credentials",
            "main.tf",
            "versions.tf",
            ".terraform.lock.hcl",
            "management/publish-timer-attestation",
        )
    }
    paths.update(units)
    sums = {
        name: hashlib.sha256(path.read_bytes()).hexdigest()
        for name, path in paths.items()
    }
    return hashlib.sha256(json.dumps(sums, sort_keys=True).encode()).hexdigest()


def scope_digest(scope):
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def cloud_items(profile, service, resource, parent):
    out = subprocess.check_output(
        [
            "nebius",
            "-p",
            profile,
            service,
            resource,
            "list",
            "--all",
            "--parent-id",
            parent,
            "--format",
            "json",
        ],
        timeout=45,
        env={**os.environ, "AI_AGENT": os.environ.get("AI_AGENT") or "worker-cleanup"},
    )
    response = json.loads(out)
    if (
        not isinstance(response, dict)
        or response.keys() - {"items", "next_page_token"}
        or response.get("next_page_token")
        or not isinstance(response.get("items", []), list)
    ):
        raise ValueError("Incomplete provider inventory for " + resource)
    entries = {}
    for item in response.get("items", []):
        ident = item["metadata"]["id"]
        if not isinstance(ident, str) or not ident or ident in entries:
            raise ValueError("Invalid provider ID for " + resource)
        entries[ident] = item
    return entries


def verify_inventory(profile, project_id, cluster_id, baseline, *, before_stage=None):
    if (baseline["project_id"], baseline["cluster_id"]) != (project_id, cluster_id):
        raise ValueError("Inventory baseline belongs to another target")
    for key, service, resource, parent, support_key in (
        ("instance_ids", "compute", "instance", project_id, "support_instance_id"),
        ("disk_ids", "compute", "disk", project_id, "support_boot_disk_id"),
        ("group_ids", "mk8s", "node-group", cluster_id, "support_group_id"),
    ):
        ids = set(cloud_items(profile, service, resource, parent))
        original = baseline[key]
        if not isinstance(original, list) or not all(
            isinstance(x, str) and x for x in original
        ):
            raise ValueError("Invalid baseline for " + key)
        expected = set(original)
        if before_stage and before_stage["stage"] == "gpu":
            extra = before_stage[support_key]
            if not isinstance(extra, str) or not extra or extra in expected:
                raise ValueError("Expected a distinct observed " + support_key)
            expected.add(extra)
        if ids - expected or (before_stage and ids != expected):
            raise ValueError("Inventory differs from approved " + key)
    print("Provider inventory reconciled against the approved IDs.", flush=True)


def terraform(*args, capture=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TF_CLI_ARGS")}
    return subprocess.run(
        ["terraform", *args],
        env=env,
        check=True,
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def plan_release(saved, *, profile, cluster_id, run_id, cutoff_label):
    groups = cloud_items(profile, "mk8s", "node-group", cluster_id)
    target_ids = {
        ident
        for ident, group in groups.items()
        if group["metadata"]["parent_id"] == cluster_id
        and group["metadata"].get("labels", {}).get("airaware-run") == run_id
        and group["metadata"].get("labels", {}).get("airaware-cutoff") == cutoff_label
    }
    # Terraform labels every targeted destroy plan incomplete. Check this private
    # plan against independently read cloud IDs, exact addresses, and run labels.
    terraform(
        "plan",
        "-destroy",
        *["-target=" + addr for addr in sorted(GROUPS)],
        "-input=false",
        "-no-color",
        "-lock=true",
        "-lock-timeout=60s",
        f"-out={saved}",
        capture=True,
    )
    plan = json.loads(terraform("show", "-json", saved, capture=True))
    release_candidates(plan, cluster_id, run_id, cutoff_label, target_ids=target_ids)
    return target_ids
