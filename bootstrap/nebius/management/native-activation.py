# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Render the signed, operator-only native Job activation. All output is local."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "workloads/sky"))
from production_release import check_native_policy, current_release
sys.path.insert(0, str(ROOT / "bootstrap/nebius"))
from worker_plan import timestamp

NAME = "episode-submit-inputs"
ROLE = "episode-native-window"
RECEIPT = "airaware.training/deployment-sha256"
STAGE = "airaware.training/native-window"
HOLD = {"expression": "false", "message": "Native training submission is closed during CPU qualification."}
SUPPORT = {
    "expression": "!variables.usesGPU || (has(request.dryRun) && request.dryRun)",
    "message": "The support-only window permits only CPU preflight Job creation.",
}


def held_policy_and_binding():
    output = subprocess.check_output(
        ["kubectl", "kustomize", str(ROOT / "platform/overlays/prod")], text=True, timeout=20,
    )
    objects = {
        obj["kind"]: obj for obj in yaml.safe_load_all(output)
        if obj["metadata"]["name"] == NAME
    }
    policy = objects["ValidatingAdmissionPolicy"]
    binding = objects["ValidatingAdmissionPolicyBinding"]
    checks = policy["spec"]["validations"]
    if (not checks or checks[0] != HOLD or checks.count(HOLD) != 1
            or policy["spec"].get("failurePolicy") != "Fail"
            or binding["spec"] != {
                "policyName": NAME, "validationActions": ["Deny"],
                "matchResources": {"namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "training"}
                }},
            }):
        raise ValueError("Expected the default native submission hold and Deny binding")
    return policy, binding


def active_policy(images, receipt, stage):
    if stage not in ("support", "gpu") or not re.fullmatch("[0-9a-f]{64}", receipt):
        raise ValueError("Expected a verified deployment digest and explicitly selected window stage")
    policy, _ = held_policy_and_binding()
    check_native_policy(images, policy)
    policy["metadata"]["annotations"] = {RECEIPT: receipt, STAGE: stage}
    checks = policy["spec"]["validations"]
    policy["spec"]["validations"] = ([SUPPORT] if stage == "support" else []) + checks[1:]
    return policy


def with_match_defaults(spec, key):
    # The API supplies these defaults without changing which requests match.
    result = dict(spec)
    match = dict(result[key])
    match.setdefault("matchPolicy", "Equivalent")
    match.setdefault("namespaceSelector", {})
    match.setdefault("objectSelector", {})
    for name in ("resourceRules", "excludeResourceRules"):
        if name in match:
            match[name] = [{"scope": "*", **rule} for rule in match[name]]
    result[key] = match
    return result


def check_active(policy, binding, live):
    supplied = live["items"]
    expected = {
        (obj["apiVersion"], obj["kind"], obj["metadata"]["name"]): obj
        for obj in (policy, binding)
    }
    if len(supplied) != len(expected):
        raise ValueError("Expected only the live native policy and its binding")
    for observed in supplied:
        identity = (observed["apiVersion"], observed["kind"], observed["metadata"]["name"])
        target = expected.pop(identity, None)
        if target is None:
            raise ValueError("Unexpected live native policy or binding identity")
        is_policy = observed["kind"] == "ValidatingAdmissionPolicy"
        match_key = "matchConstraints" if is_policy else "matchResources"
        if with_match_defaults(observed["spec"], match_key) != with_match_defaults(target["spec"], match_key):
            raise ValueError("Live native policy or Deny binding differs from the reviewed object")
        if is_policy:
            annotations = observed["metadata"].get("annotations", {})
            intended = policy["metadata"].get("annotations", {})
            if any(annotations.get(key) != intended.get(key) for key in (RECEIPT, STAGE)):
                raise ValueError("Live native policy is not annotated for this deployment and stage")
            generation = observed["metadata"].get("generation", 0)
            status = observed.get("status", {})
            checking = status.get("typeChecking")
            if (generation < 1 or status.get("observedGeneration") != generation
                    or not isinstance(checking, dict) or checking.get("expressionWarnings")):
                raise ValueError("Live native policy lacks current successful CEL type checking")


def access_objects(receipt):
    resource = {"apiVersion": "rbac.authorization.k8s.io/v1",
                "metadata": {"name": ROLE, "namespace": "training", "annotations": {RECEIPT: receipt}}}
    role = {**resource, "kind": "Role", "rules": [
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]}
    binding = {
        **resource, "kind": "RoleBinding",
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ROLE},
        "subjects": [{"kind": "ServiceAccount", "name": "episode-submit", "namespace": "training"}],
    }
    return {"apiVersion": "v1", "kind": "List", "items": [role, binding]}


def require_window(scope_file, stage, cluster, now):
    mode = scope_file.stat()
    if mode.st_mode & 0o077 or mode.st_uid != os.getuid():
        raise ValueError("Expected an owner-only approved window scope")
    scope = json.loads(scope_file.read_bytes())
    start, cutoff = timestamp(scope["start"]), timestamp(scope["cutoff"])
    if (scope["stage"] != stage or scope["cluster_id"] != cluster
            or not start <= now < cutoff - timedelta(minutes=10)):
        raise ValueError("Expected the matching active worker scope with time for the bounded session")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("held-policy", help="Render the checked-in unconditional recovery hold")
    commands.add_parser("check-held", help="Verify the live unconditional hold and Deny binding JSON on stdin")
    for name in ("policy", "access"):
        mode = commands.add_parser(name)
        mode.add_argument("--stage", choices=("support", "gpu"), required=True)
        mode.add_argument("--expected-receipt", required=True)
        mode.add_argument("--scope", type=Path, required=True)
    args = parser.parse_args()
    held, binding = held_policy_and_binding()
    if args.command == "held-policy":
        output = held
    elif args.command == "check-held":
        check_active(held, binding, json.load(sys.stdin))
        return
    else:
        require_window(args.scope, args.stage, os.environ["NEBIUS_TRAINING_CLUSTER_ID"], datetime.now(timezone.utc))
        images, receipt = current_release()
        if args.expected_receipt != receipt:
            raise ValueError("Signed deployment changed or differs from the independently approved receipt")
        policy = active_policy(images, receipt, args.stage)
        if args.command == "policy":
            output = policy
        else:
            check_active(policy, binding, json.load(sys.stdin))
            output = access_objects(receipt)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError) as error:
        sys.exit(f"Native activation refused: {error}")
