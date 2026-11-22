"""Create one independently approved public staging Job during an exclusive storage handoff."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import uuid

from private_api import pinned_kubeconfig_ca
from production_target import kubectl_client_for_identity


HERE = Path(__file__).resolve().parent
SCOPE_SHA256 = "a82b7ef7bd96295b9ce33fa10d629be2e19b1c9f8dd0b55f0c513921804be7f6"


def approved_packet(path, pin, reproduced):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != pin or json.loads(raw) != reproduced:
        raise ValueError("Review file differs from the independent approval or reproduced render")
    return reproduced


def check_placement(placement, node):
    meta, spec, status = node["metadata"], node["spec"], node["status"]
    if (meta.get("uid") != placement["uid"] or meta["name"] != placement["name"]
            or meta.get("deletionTimestamp") or spec.get("unschedulable")
            or spec.get("providerID") != placement["provider_id"]
            or meta["labels"].get("airaware.training/pool") != "training-cpu"
            or status["allocatable"].get("nvidia.com/gpu", "0") != "0"
            or not any(c["type"] == "Ready" and c["status"] == "True" for c in status["conditions"])):
        raise ValueError("Expected the original Ready managed CPU node")


def create(kube, document):
    kind, meta = document["kind"], document["metadata"]
    result = subprocess.run([*kube, "--request-timeout=30s", "create", "-f", "-", "-o", "json"],
                            input=json.dumps(document), text=True, capture_output=True, timeout=35)
    if result.returncode == 0:
        obj = json.loads(result.stdout)
        if (obj["kind"] == kind and obj["metadata"]["name"] == meta["name"]
                and obj["metadata"]["namespace"] == meta["namespace"] and obj["metadata"].get("uid")):
            return obj["metadata"]["uid"]
    raise RuntimeError(f"Creation was not confirmed; inspect {kind} {meta['name']} before retry")


def check_time(cutoff, deadline, now):
    # The original installed window and the owner's shorter handoff expiry both apply.
    if now + timedelta(seconds=deadline + 120) >= cutoff:
        raise ValueError("Insufficient time for the reviewed CPU Job and writer closeout")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for key in ("packet", "approval", "promotion", "binding", "scope"):
        cli.add_argument("--" + key, type=Path, required=True)
    cli.add_argument("--approved-sha256", required=True, help="Independent owner's approval file SHA-256")
    a = cli.parse_args()
    raw = a.approval.read_bytes()
    if hashlib.sha256(raw).hexdigest() != a.approved_sha256 or a.approval.stat().st_mode & 0o077:
        raise ValueError("Expected the private independently approved handoff")
    approval = json.loads(raw)
    if (approval["purpose"] != "public-flamingo-stage" or approval["exclusive_storage_handoff"] is not True
            or approval["inventory_secret_immutable"] is not True):
        raise ValueError("Owner must grant the exclusive handoff and approve the immutable inventory")
    raw_scope = a.scope.read_bytes()
    if hashlib.sha256(raw_scope).hexdigest() != SCOPE_SHA256:
        raise ValueError("Expected the original allocation scope")
    scope = json.loads(raw_scope)
    inventory_uid = str(uuid.UUID(approval["inventory_uid"]))
    render = [sys.executable, str(HERE / "render-public-flamingo-stage.py"), "--promotion", str(a.promotion),
              "--binding", str(a.binding), "--run", approval["run"], "--expected-deployment",
              approval["deployment_sha256"], "--inventory-secret", approval["inventory_secret"]]

    def reproduce():
        return approved_packet(a.packet, approval["packet_file_sha256"],
                               json.loads(subprocess.check_output(render, timeout=30)))

    packet = reproduce()
    operator = approval["staging_operator"]
    storage_operator = os.environ["NEBIUS_STORAGE_OPERATOR_IDENTITY"]
    if (operator in ("", "system:anonymous", storage_operator)
            or operator.startswith("system:serviceaccount:training:") and operator.rsplit(":", 1)[-1] in
            {"episode-worker", "episode-submit", "episode-input-stager", "default"}
            or storage_operator != approval["storage_operator"]
            or os.environ["NEBIUS_TRAINING_CLUSTER_ID"] != packet["cluster_id"]
            or scope["cluster_id"] != packet["cluster_id"] or scope["project_id"] != packet["project_id"]):
        raise ValueError("Expected independently enrolled staging and storage operators on the approved target")
    kube = kubectl_client_for_identity("training", operator, direct=True)
    owner_expiry = datetime.fromisoformat(approval["expires_at"])
    if owner_expiry.tzinfo is None:
        raise ValueError("Owner expiry requires an explicit UTC offset")
    cutoff = min(owner_expiry, datetime.fromisoformat(scope["cutoff"].replace("Z", "+00:00")))
    deadline = packet["job"]["spec"]["activeDeadlineSeconds"]

    # Controller inventory and the native hold are observed with the enrolled storage reader.
    storage_config = Path(os.environ["NEBIUS_STORAGE_KUBECONFIG"])
    if storage_config.stat().st_mode & 0o077:
        raise ValueError("Expected a private storage kubeconfig")
    observer = ["kubectl", "--kubeconfig", str(storage_config), "--context", "prod-nebius"]
    pinned_kubeconfig_ca(observer, packet["cluster_id"], os.environ["NEBIUS_CA_BUNDLE_SHA256"],
                         endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"), direct=True)
    if subprocess.check_output([*observer, "--request-timeout=10s", "auth", "whoami", "-o",
                                "jsonpath={.status.userInfo.username}"], text=True, timeout=15) != storage_operator:
        raise ValueError("Unexpected storage observer")

    def get(*args):
        return json.loads(subprocess.check_output([*observer, "--request-timeout=10s", "get", *args, "-o", "json"],
                                                  timeout=15))

    closeout = runpy.run_path(str(HERE / "public-flamingo-stage-closeout.py"))
    native = runpy.run_path(str(HERE.parents[1] / "bootstrap/nebius/management/native-activation.py"))

    def gates(own_job=None):
        check_time(cutoff, deadline, datetime.now(timezone.utc))
        native["check_active"](*native["held_policy_and_binding"](),
                               {"items": [get(native["POLICY"]), get(native["GUARD"])]})
        closeout["exclude_other_writers"](get, own_job)
        node = get("node", packet["placement"]["name"])
        check_placement(packet["placement"], node)
        # Print only identity and immutability; never fetch Secret data into a file or transcript.
        actual = subprocess.check_output([*kube, "--request-timeout=10s", "get", "secret", approval["inventory_secret"],
                                          "-o", 'jsonpath={.metadata.uid}{" "}{.immutable}{" "}{.metadata.deletionTimestamp}'],
                                         text=True, timeout=15)
        if actual.strip() != inventory_uid + " true":
            raise ValueError("Expected the approved immutable inventory Secret UID")
        env = dict(os.environ, NEBIUS_STORAGE_BINDING_RECEIPT=str(a.binding),
                   NEBIUS_STORAGE_BINDING_SHA256=packet["binding_sha256"])
        subprocess.run([sys.executable, str(HERE / "training-storage-handoff.py")], env=env, check=True, timeout=140)
        check_time(cutoff, deadline, datetime.now(timezone.utc))

    gates()
    program_uid = create(kube, packet["configmap"])
    # If admission fails or a read races with retirement, leave the named object for attended reconciliation.
    print(f"Created {packet['configmap']['metadata']['name']} UID {program_uid}", file=sys.stderr, flush=True)
    reproduce()
    gates()
    program = get("configmap", packet["configmap"]["metadata"]["name"], "-n", "training")
    if (program["metadata"]["uid"] != program_uid or program["metadata"].get("deletionTimestamp")
            or program.get("immutable") is not True or program["data"] != packet["configmap"]["data"]
            or program.get("binaryData")):
        raise ValueError("Approved stage program changed after creation")
    job = packet["job"]
    enrollment = {**job, "spec": {**job["spec"], "suspend": True}}
    job_uid = create(kube, enrollment)
    name = job["metadata"]["name"]
    print(f"Enrolled suspended {name} UID {job_uid}", file=sys.stderr, flush=True)
    admitted = get("job", name, "-n", "training")
    closeout["checked_meta"](admitted, name, job_uid)
    closeout["verify_job"](packet, admitted, suspended=True)
    gates(job_uid)
    reproduce()
    admitted = get("job", name, "-n", "training")
    metadata = closeout["checked_meta"](admitted, name, job_uid)
    if metadata.get("ownerReferences") or admitted.get("status", {}).get("active"):
        raise ValueError("Expected one independently controlled suspended enrollment")
    closeout["verify_job"](packet, admitted, suspended=True)
    patch = [{"op": "test", "path": "/metadata/uid", "value": job_uid},
             {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
             {"op": "test", "path": "/spec/suspend", "value": True},
             {"op": "replace", "path": "/spec/suspend", "value": False}]
    result = subprocess.run([*kube, "--request-timeout=30s", "patch", "job", name, "--type=json",
                             "--patch", json.dumps(patch), "-o", "json"], capture_output=True, timeout=35)
    if result.returncode:
        raise RuntimeError(f"Activation was not confirmed; inspect Job {name} UID {job_uid} before retry")
    active = json.loads(result.stdout)
    closeout["checked_meta"](active, name, job_uid)
    closeout["verify_job"](packet, active)
    canonical = json.dumps(packet, sort_keys=True, separators=(",", ":")).encode()
    print(json.dumps({"job_uid": job_uid, "configmap_uid": program_uid, "inventory_uid": inventory_uid,
                      "packet_file_sha256": approval["packet_file_sha256"],
                      "packet_sha256": hashlib.sha256(canonical).hexdigest()}, sort_keys=True))


if __name__ == "__main__":
    main()
