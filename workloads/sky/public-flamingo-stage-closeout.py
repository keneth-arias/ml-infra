#!/usr/bin/env python3
"""Collect a completed public stage and independently fence its retired writer."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from uuid import UUID

from private_api import pinned_kubeconfig_ca

HERE = Path(__file__).resolve().parent
HANDOFF = runpy.run_path(str(HERE / "training-storage-handoff.py"))
NS = "training"
CLAIM = HANDOFF["CLAIM"]
# Signed release 787788ff7c59, preflight.images linux/amd64 manifest. Some
# containerd builds report this ref while others report its signed OCI index.
PLATFORM_IMAGE = ("ghcr.io/keneth-arias/ml-infra-qwen@"
                  "sha256:de70714673980521ffd8a4e9704b9ae21f476574df7cc1a5c4c96f32d7b7f492")


def expected_image_id(image_id, packet):
    return image_id.removeprefix("docker-pullable://") in (packet["image"], PLATFORM_IMAGE)



def digest(record):
    return sha256(json.dumps(record, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def uid(value):
    if str(UUID(value)) != value:
        raise ValueError("Expected a canonical Kubernetes UID")
    return value


def checked_meta(resource, name, expected_uid):
    meta = resource["metadata"]
    if ((meta["name"], meta["namespace"], meta["uid"]) != (name, NS, expected_uid)
            or meta.get("deletionTimestamp")):
        raise ValueError("Enrolled resource identity changed or is retiring")
    return meta


def complete(job):
    return any(c["type"] == "Complete" and c["status"] == "True"
               for c in job.get("status", {}).get("conditions", []))


def inventory(result):
    if result.get("metadata", {}).get("continue"):
        raise ValueError("Incomplete Kubernetes inventory")
    return result["items"]


def consumes(spec):
    return any(v.get("persistentVolumeClaim", {}).get("claimName") == CLAIM for v in spec.get("volumes", []))


def exclude_other_writers(get, own_job=None):
    for job in inventory(get("jobs", "-n", NS)):
        if job["metadata"]["uid"] == own_job or not consumes(job["spec"]["template"]["spec"]):
            continue
        terminal = any(c["type"] in {"Complete", "Failed"} and c["status"] == "True"
                       for c in job.get("status", {}).get("conditions", []))
        if not terminal or job.get("status", {}).get("active"):
            raise ValueError("Another Job can attach the training claim")
    for kind in ("cronjobs", "deployments", "replicasets", "statefulsets", "daemonsets", "replicationcontrollers"):
        for owner in inventory(get(kind, "-n", NS)):
            spec = owner["spec"]
            if kind == "cronjobs":
                spec = spec["jobTemplate"]["spec"]
            if consumes(spec["template"]["spec"]):
                raise ValueError("A workload controller can attach the training claim")


def defaulted(actual, expected, defaults):
    for key, value in defaults.items():
        if key not in expected and actual.get(key) == value:
            del actual[key]


def verify_job(packet, job, suspended=False):
    expected = packet["job"]["spec"]
    spec = deepcopy(job["spec"])
    name, controller = job["metadata"]["name"], job["metadata"]["uid"]
    defaults = {"parallelism": 1, "completions": 1, "suspend": False, "manualSelector": False,
                "completionMode": "NonIndexed", "podReplacementPolicy": "TerminatingOrFailed",
                "selector": {"matchLabels": {"batch.kubernetes.io/controller-uid": controller}}}
    defaulted(spec, expected, defaults)
    template = spec["template"]
    meta = template["metadata"]
    generated = {"job-name": name, "batch.kubernetes.io/job-name": name,
                 "controller-uid": controller, "batch.kubernetes.io/controller-uid": controller}
    if any(meta["labels"].get(k) != v for k, v in generated.items()):
        raise ValueError("Job controller labels differ")
    template_labels = expected["template"]["metadata"]["labels"]
    job_labels = dict(job["metadata"]["labels"])
    for key, value in generated.items():
        if job_labels.get(key) == value:
            del job_labels[key]
    if job_labels != packet["job"]["metadata"]["labels"] or job["metadata"].get("annotations"):
        raise ValueError("Admitted Job labels or annotations differ from review")
    if suspended and spec.pop("suspend", None) is not True:
        raise ValueError("Expected a suspended enrollment")
    if "creationTimestamp" in meta and meta["creationTimestamp"] is None:
        del meta["creationTimestamp"]
    for key in generated:
        del meta["labels"][key]
    expected_template = expected["template"]
    wanted = expected_template["spec"]
    job_pod = template["spec"]
    # The API mirrors serviceAccountName to its deprecated alias.
    defaulted(job_pod, wanted, {"serviceAccount": wanted["serviceAccountName"]})
    if spec != expected:
        raise ValueError("Admitted Job differs from the reviewed Job")
    return {**template_labels, **generated}


def verify_specs(packet, job, pod):
    labels = verify_job(packet, job)
    pod_labels = dict(pod["metadata"]["labels"])
    if pod_labels.get("topology.kubernetes.io/region") == "eu-south1":
        del pod_labels["topology.kubernetes.io/region"]
    if pod_labels != labels or pod["metadata"].get("annotations"):
        raise ValueError("Executed Pod labels or annotations differ from review")
    wanted = packet["job"]["spec"]["template"]["spec"]
    observed = deepcopy(pod["spec"])
    if observed.pop("nodeName") != packet["placement"]["name"]:
        raise ValueError("Unexpected CPU stage placement")
    defaults = {"serviceAccount": wanted["serviceAccountName"], "priority": 0, "preemptionPolicy": "PreemptLowerPriority"}
    defaulted(observed, wanted, defaults)
    # Permit only the standard node-controller tolerations added to Pods.
    ordinary = [{"effect": "NoExecute", "key": "node.kubernetes.io/" + key,
                 "operator": "Exists", "tolerationSeconds": 300} for key in ("not-ready", "unreachable")]
    observed_tolerations = observed.get("tolerations", [])
    for toleration in ordinary:
        if toleration not in wanted.get("tolerations", []) and toleration in observed_tolerations:
            observed_tolerations.remove(toleration)
    if not observed_tolerations and "tolerations" not in wanted:
        observed.pop("tolerations", None)
    if observed != wanted:
        raise ValueError("Executed Pod differs from the reviewed template")


def worker_facts(packet):
    return {"weights_manifest_sha256": packet["weights_manifest_sha256"], "output_uid": 1000, "output_empty": True}


def capture(packet, ids, get, logs, secret_meta):
    job_name = packet["job"]["metadata"]["name"]
    job = get("job", job_name, "-n", NS)
    checked_meta(job, job_name, ids["job"])
    if (not complete(job) or job["status"].get("succeeded") != 1 or job["status"].get("active")
            or job["status"].get("failed") or job["metadata"].get("ownerReferences")):
        raise ValueError("Expected one successful standalone stage Job")
    cm_name = packet["configmap"]["metadata"]["name"]
    cm = get("configmap", cm_name, "-n", NS)
    checked_meta(cm, cm_name, ids["configmap"])
    if cm.get("immutable") is not True or cm.get("binaryData") or cm["data"] != packet["configmap"]["data"]:
        raise ValueError("Immutable stage program differs from review")
    secret = secret_meta(job_name + "-inventory")
    checked_meta(secret, job_name + "-inventory", ids["inventory"])
    if secret.get("immutable") is not True or secret.get("type") != "Opaque" or secret["keys"] != ["inventory.json"]:
        raise ValueError("Expected the enrolled immutable source inventory")
    children = []
    for pod in inventory(get("pods", "-n", NS)):
        owners = pod["metadata"].get("ownerReferences", [])
        if any(owner.get("uid") == ids["job"] for owner in owners):
            children.append(pod)
        elif consumes(pod["spec"]) and pod.get("status", {}).get("phase") not in {"Failed", "Succeeded"}:
            raise ValueError("Another Pod can write the training claim")
    if len(children) != 1:
        raise ValueError("Expected one executed Pod for the enrolled Job")
    pod = children[0]
    pm = pod["metadata"]
    checked_meta(pod, pm["name"], uid(pm["uid"]))
    owners = pm["ownerReferences"]
    if (len(owners) != 1 or (owners[0]["apiVersion"], owners[0]["kind"], owners[0]["name"], owners[0].get("controller"))
            != ("batch/v1", "Job", job_name, True)):
        raise ValueError("Pod ownership differs from the enrolled Job")
    verify_specs(packet, job, pod)
    node = get("node", pod["spec"]["nodeName"])
    place = packet["placement"]
    if ((node["metadata"]["name"], node["metadata"]["uid"], node["spec"]["providerID"])
            != (place["name"], place["uid"], place["provider_id"])
            or node["metadata"]["labels"].get("airaware.training/pool") != "training-cpu"
            or node["metadata"].get("deletionTimestamp")
            or not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])):
        raise ValueError("CPU node does not match the enrolled placement")
    statuses = pod["status"]["containerStatuses"]
    if pod["status"]["phase"] != "Succeeded" or len(statuses) != 1:
        raise ValueError("Stage Pod did not terminate successfully")
    container = statuses[0]
    name = pod["spec"]["containers"][0]["name"]
    ended = container["state"]["terminated"]
    if (container["name"] != name or container["restartCount"] != 0 or ended["exitCode"] != 0
            or not ended.get("finishedAt") or not expected_image_id(container["imageID"], packet)):
        raise ValueError("Container termination or executed image differs from review")
    result = json.loads(logs(pm["name"], name))
    if digest(result) != digest(worker_facts(packet)):
        raise ValueError("Unexpected stage output")
    # A log read addresses a Pod name. Fence name reuse on both sides of that read.
    checked_meta(get("pod", pm["name"], "-n", NS), pm["name"], pm["uid"])
    checked_meta(get("job", job_name, "-n", NS), job_name, ids["job"])
    exclude_other_writers(get, ids["job"])
    return {"schema": "public-flamingo-stage-capture-v1", "cluster_id": packet["cluster_id"],
            "packet_sha256": digest(packet), "run": packet["run"], "uids": {**ids, "pod": pm["uid"]},
            "pod_name": pm["name"], "job_name": job_name, "configmap_name": cm_name,
            "inventory_name": job_name + "-inventory", "placement": place, "image_id": container["imageID"],
            "finished_at": ended["finishedAt"], "observed_at": datetime.now(timezone.utc).isoformat(),
            "worker": result}


def fence(packet, ids, observation, get, absent, detached):
    if (observation["schema"] != "public-flamingo-stage-capture-v1" or observation["packet_sha256"] != digest(packet)
            or observation["cluster_id"] != packet["cluster_id"] or observation["run"] != packet["run"]
            or not expected_image_id(observation["image_id"], packet) or observation["placement"] != packet["placement"]
            or digest(observation["worker"]) != digest(worker_facts(packet))):
        raise ValueError("Capture does not bind the approved packet")
    if observation["uids"] != {**ids, "pod": uid(observation["uids"]["pod"])}:
        raise ValueError("Capture does not bind the enrolled objects")
    job = packet["job"]["metadata"]["name"]
    for kind, name in (("job", job), ("pod", observation["pod_name"]), ("configmap", packet["configmap"]["metadata"]["name"]),
                       ("secret", job + "-inventory")):
        if not absent(kind, name):
            raise ValueError("Owner retirement is incomplete or a name has been reused")
    for pod in inventory(get("pods", "-n", NS)):
        if any(o.get("uid") == ids["job"] for o in pod["metadata"].get("ownerReferences", [])):
            raise ValueError("A Pod for the retired Job remains")
    exclude_other_writers(get)
    if not detached():
        raise ValueError("Training disk attachment or consumer remains")
    return {"cluster_id": packet["cluster_id"], "run": packet["run"], "disk_id": packet["binding"]["provider_disk_id"],
            "output_subpath": "flamingo-baselines/" + packet["run"], **worker_facts(packet), "writer_fenced": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "fence"))
    for opt in ("promotion", "binding"):
        parser.add_argument("--" + opt, type=Path, required=True)
    for opt in ("expected-deployment", "run", "packet-sha256"):
        parser.add_argument("--" + opt, required=True)
    for opt in ("job", "configmap", "inventory"):
        parser.add_argument("--" + opt + "-uid", required=True, type=uid)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--capture-sha256")
    a = parser.parse_args()
    if (a.capture is not None) != (a.action == "fence") or bool(a.capture_sha256) != (a.action == "fence"):
        parser.error("Pass --capture and its independently recorded byte SHA-256 for fence")
    render = runpy.run_path(str(HERE / "render-public-flamingo-stage.py"))
    packet = render["build_packet"](a.promotion, a.binding, a.expected_deployment, a.run, "stage-" + a.run + "-inventory")
    cluster = os.environ["NEBIUS_TRAINING_CLUSTER_ID"]
    if digest(packet) != a.packet_sha256 or packet["cluster_id"] != cluster:
        raise ValueError("Packet or enrolled cluster differs from approval")
    profile = Path(os.environ["NEBIUS_STORAGE_KUBECONFIG"])
    identity = os.environ["NEBIUS_STORAGE_OPERATOR_IDENTITY"]
    if not identity or identity in ("system:anonymous", HANDOFF["SUBMITTER"]) or profile.stat().st_mode & 0o077:
        raise ValueError("Select the enrolled private storage operator context")
    kube = ["kubectl", "--context", "prod-nebius", "--kubeconfig", str(profile)]
    pinned_kubeconfig_ca(kube, cluster, os.environ["NEBIUS_CA_BUNDLE_SHA256"], direct=True,
                         endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"))

    def read(*cmd):
        return subprocess.check_output([*kube, "--request-timeout=10s", *cmd], timeout=15, stderr=subprocess.DEVNULL)

    if read("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}").decode() != identity:
        raise ValueError("Unexpected managed storage operator")
    project, binding = HANDOFF["approved_binding"](a.binding, packet["binding_sha256"], cluster)
    if project != packet["project_id"]:
        raise ValueError("Managed project differs from packet")

    def get(*args):
        return json.loads(read("get", *args, "-o", "json"))

    def detached():
        current, idle = HANDOFF["status"](get)
        if current != binding:
            raise ValueError("Managed disk binding changed")
        if not idle:
            return False
        disk = json.loads(subprocess.check_output(["nebius", "-p", "ml-prod", "--no-browser", "--no-check-update",
            "--no-progress", "--format", "json", "compute", "disk", "get", binding[2]], timeout=15,
            env={**os.environ, "AI_AGENT": os.environ.get("AI_AGENT") or "codex"}))
        return HANDOFF["provider_detached"](disk, project, binding[2])

    if HANDOFF["status"](get)[0] != binding:
        raise ValueError("Managed training claim identity differs from approval")
    native = runpy.run_path(str(HERE.parents[1] / "bootstrap/nebius/management/native-activation.py"))
    native["check_active"](*native["held_policy_and_binding"](), {"items": [get(native["POLICY"]), get(native["GUARD"])]})
    ids = {"job": a.job_uid, "configmap": a.configmap_uid, "inventory": a.inventory_uid}
    if a.action == "capture":
        def secret_meta(name):
            # Only return UID, lifecycle, type and key names across the process boundary.
            template = ('{{.metadata.name}} {{.metadata.namespace}} {{.metadata.uid}} {{if .metadata.deletionTimestamp}}'
                        'deleting{{else}}active{{end}} {{.immutable}} {{.type}}{{range $k,$v := .data}} {{$k}}{{end}}')
            nm, ns, ident, lifecycle, immutable, typ, *keys = read(
                "get", "secret", name, "-n", NS, "-o", "go-template=" + template).decode().split()
            return {"metadata": {"name": nm, "namespace": ns, "uid": ident, "deletionTimestamp": lifecycle != "active"},
                    "immutable": immutable == "true", "type": typ, "keys": keys}
        result = capture(packet, ids, get, lambda pod, name: read("logs", pod, "-c", name, "-n", NS, "--limit-bytes=8192"),
                         secret_meta)
    else:
        if a.capture.stat().st_mode & 0o077:
            raise ValueError("Use an owner-only capture record")
        raw = a.capture.read_bytes()
        if sha256(raw).hexdigest() != a.capture_sha256:
            raise ValueError("Capture bytes differ from the reviewed digest")
        def absent(kind, name):
            return not read("get", kind, name, "-n", NS, "--ignore-not-found", "-o", "name").strip()
        result = fence(packet, ids, json.loads(raw), get, absent, detached)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as exc:
        # Do not emit diagnostics containing a private inventory or worker log.
        sys.exit("Public stage closeout failed: " + (str(exc) if type(exc) is ValueError else type(exc).__name__))
