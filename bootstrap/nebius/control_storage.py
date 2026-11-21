"""Storage destinations for a funded worker window and its release proofs."""

import json


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
