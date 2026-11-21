"""Storage destinations for a funded worker window and its release proofs."""

import json
import os
import subprocess


LEDGER_KEY = "prod/mk8s/release-control/ledger.json"
STORAGE_FIELDS = ("project_id", "control_project_id", "state_bucket", "control_bucket")


def control_bucket(scope):
    state, control = (scope.get(field) for field in ("state_bucket", "control_bucket"))
    if not all(isinstance(name, str) and name and name == name.strip() for name in (state, control)):
        raise ValueError("Specify both state_bucket and control_bucket")
    if state.casefold() == control.casefold():
        raise ValueError("control_bucket must be distinct from state_bucket")
    project, management = (scope.get(field) for field in ("project_id", "control_project_id"))
    if not all(isinstance(ident, str) and ident and ident == ident.strip() for ident in (project, management)):
        raise ValueError("Specify both project_id and control_project_id")
    if project == management:
        raise ValueError("control_project_id must be distinct from project_id")
    return control


def verify_bucket_projects(scope):
    control_bucket(scope)
    env = {**os.environ, "AI_AGENT": os.environ.get("AI_AGENT") or "worker-allocation"}
    for field, owner in (
        ("state_bucket", "project_id"), ("control_bucket", "control_project_id")
    ):
        name, project = scope[field], scope[owner]
        command = [
            "nebius", "-p", "ml-prod", "--no-browser", "--no-check-update", "storage",
            "bucket", "get-by-name", "--name", name, "--parent-id", project, "--format", "json",
        ]
        try:
            metadata = json.loads(subprocess.check_output(command, env=env, timeout=30))["metadata"]
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Could not verify the owning project for {field}") from exc
        if not isinstance(metadata, dict) or (metadata.get("name"), metadata.get("parent_id")) != (name, project):
            raise ValueError(f"Unexpected owning project or name for {field}")


def timer_prefix(host):
    return f"prod/mk8s/release-control/timers/{host}/"


def timer_key(host, run, stage):
    return f"{timer_prefix(host)}{run}/{stage}.json"


def publish_timer_proof(s3, scope, host, proof):
    bucket = control_bucket(scope)
    proof = {**proof, **{field: scope[field] for field in STORAGE_FIELDS}}
    response = s3.put_object(
        Bucket=bucket,
        Key=timer_key(host, scope["run_id"], scope["stage"]),
        ContentType="application/json",
        Body=json.dumps(proof).encode(),
    )
    version = response.get("VersionId")
    if not isinstance(version, str) or version in ("", "null"):
        raise ValueError("Expected a versioned timer attestation")
    return version
