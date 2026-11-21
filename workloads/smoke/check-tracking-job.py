"""Qualify tracking on one existing Nebius worker using the signed trainer and a synthetic run."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sky"))
from production_release import current_release
from production_target import bind_tracking_client, kubectl_client_for_identity


def render(image, node, pool, name):
    pool_label = "training-" + pool
    pod = {
        "serviceAccountName": "episode-worker", "automountServiceAccountToken": False,
        "restartPolicy": "Never", "nodeSelector": {"airaware.training/pool": pool_label},
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}]}}},
        "imagePullSecrets": [{"name": "ghcr-pull"}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}],
        "containers": [{
            "name": "tracking", "image": image,
            "command": ["/opt/conda/bin/python", "-I", "-c", (HERE / "tracking-roundtrip.py").read_text()],
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True},
            "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            "env": [
                {"name": "HOME", "value": "/tmp"}, {"name": "MLFLOW_DISABLE_TELEMETRY", "value": "true"},
                {"name": "MLFLOW_HTTP_REQUEST_TIMEOUT", "value": "5"},
                {"name": "MLFLOW_HTTP_REQUEST_MAX_RETRIES", "value": "0"},
                {"name": "TRACKING_PROBE_NODE", "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}},
            ],
        }],
    }
    pod["containers"][0]["env"].extend(bind_tracking_client(pod))
    if pool == "gpu":
        pod["tolerations"] = [{"key": "nvidia.com/gpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"}]
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "training"},
            "spec": {"activeDeadlineSeconds": 300, "backoffLimit": 0, "ttlSecondsAfterFinished": 3600,
                     "template": {"metadata": {"labels": {"app": "airaware-episodes", "airaware.training/phase": "tracking-check"}}, "spec": pod}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True, choices=("cpu", "gpu"))
    parser.add_argument("--node", required=True, help="Kubernetes node name from the funded worker receipt")
    parser.add_argument("--execute", action="store_true", help="Run in the funded worker window; default is server dry-run")
    args = parser.parse_args()
    if len(args.node) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", label)
                                      for label in args.node.split(".")):
        parser.error("Supply a Kubernetes node name from the allocation receipt")
    identity = os.environ["NEBIUS_STAGING_OPERATOR_IDENTITY"]
    if identity in ("", "system:anonymous", "system:serviceaccount:training:episode-submit"):
        raise ValueError("Use the separate qualification operator")
    images, approval = current_release()
    client = kubectl_client_for_identity("training", identity, direct=True)
    def call(*argv, body=None, timeout=30):
        return subprocess.check_output([*client, *argv], input=json.dumps(body) if body else None, text=True, timeout=timeout)

    node = json.loads(call("get", "node", args.node, "-o", "json"))
    provider = node["spec"]["providerID"]
    if (node["metadata"]["name"] != args.node or node["metadata"]["labels"].get("airaware.training/pool") != "training-" + args.pool
            or not provider or node["spec"].get("unschedulable") or not any(
                c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])):
        raise ValueError("Expected a schedulable, Ready worker in the selected pool with a provider ID")
    name = "tracking-check-" + uuid.uuid4().hex[:16]
    job = render(images["trainer"], args.node, args.pool, name)
    if current_release() != (images, approval):
        raise ValueError("Deployment changed before tracking qualification")
    print(f"{'Executing' if args.execute else 'Validating'} training Job {name}", file=sys.stderr, flush=True)
    created = json.loads(call("create", *([] if args.execute else ["--dry-run=server"]), "-f", "-", "-o", "json", body=job))
    receipt = {"job": name, "node": args.node, "provider_id": provider, "deployment_sha256": approval, "image": images["trainer"]}
    if args.execute:
        uid = created["metadata"]["uid"]
        call("wait", "--for=condition=complete", "--timeout=320s", "job/" + name, timeout=330)
        pods = json.loads(call("get", "pods", "-l", "batch.kubernetes.io/controller-uid=" + uid, "-o", "json"))["items"]
        pod, = pods
        container, = pod["status"]["containerStatuses"]
        owners = [o for o in pod["metadata"]["ownerReferences"] if o.get("controller")]
        if (len(owners) != 1 or owners[0]["uid"] != uid or owners[0]["kind"] != "Job" or owners[0]["name"] != name
                or pod["spec"]["nodeName"] != args.node or pod["status"]["phase"] != "Succeeded"
                or pod["spec"]["containers"][0]["image"] != images["trainer"]
                or container["name"] != "tracking" or container["state"]["terminated"]["exitCode"] != 0
                or not container["imageID"]):
            raise ValueError("Completed Pod differs from the tracking Job")
        # MLflow may log progress on stderr; kubectl combines the container streams.
        record = json.loads(call("logs", pod["metadata"]["name"], "-c", "tracking").strip().splitlines()[-1])
        if (record["node"] != args.node or record["mode"] != "roundtrip"
                or not re.fullmatch("[0-9a-f]{64}", record["artifact_sha256"])
                or not re.fullmatch("[0-9a-f]{32}", record["run_id"])
                or any(record[key] not in (401, 403) for key in ("anonymous_http", "invalid_bearer_http",
                                                               "artifact_anonymous_http", "artifact_invalid_bearer_http"))):
            raise ValueError("Incomplete tracking round-trip receipt")
        receipt.update(job_uid=uid, pod_uid=pod["metadata"]["uid"], image_id=container["imageID"], tracking=record)
    print(json.dumps(dict(receipt, executed=args.execute)))


if __name__ == "__main__":
    main()
