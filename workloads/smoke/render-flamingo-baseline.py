"""Render a pending Gemma baseline Job for review on the verified Nebius target."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "sky"), str(HERE.parents[1] / "runtime")]
from episode_job import FLAMINGO_WEIGHTS
from production_release import PAIRED, current_release
from production_target import kubectl_client_for_identity


def render(image, run, node, deadline, gpu_bytes):
    if not re.fullmatch(re.escape(PAIRED) + r"@sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected an immutable paired trainer image")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", run):
        raise ValueError("Invalid baseline run name")
    if len(node) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                                  for part in node.split(".")):
        raise ValueError("Invalid worker node name")
    if not 0 < deadline <= 3600 or not 0 < gpu_bytes <= 96_000_000_000:
        raise ValueError("Expected a deadline within one hour and GPU allocator budget within 96 GB")
    labels = {"airaware.training/run": run, "airaware.training/phase": "flamingo-baseline"}
    pod = {
        "serviceAccountName": "episode-worker", "automountServiceAccountToken": False,
        "restartPolicy": "Never", "terminationGracePeriodSeconds": 30,
        "nodeSelector": {"airaware.training/pool": "training-gpu"},
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchFields": [{"key": "metadata.name", "operator": "In", "values": [node]}]}]}}},
        "tolerations": [{"key": "nvidia.com/gpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"}],
        "imagePullSecrets": [{"name": "ghcr-pull"}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "volumes": [{"name": "inputs", "persistentVolumeClaim": {"claimName": "airaware-checkpoints"}},
                    {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}}],
        "containers": [{
            "name": "baseline", "image": image,
            "command": ["/opt/conda/bin/python", "-I", "-c", (HERE / "flamingo-baseline-worker.py").read_text(),
                        image.split("@")[1]],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "2", "memory": "12G", "nvidia.com/gpu": "1"},
                          "limits": {"cpu": "4", "memory": "32Gi", "nvidia.com/gpu": "1"}},
            "env": [{"name": "HF_HUB_OFFLINE", "value": "1"}, {"name": "HOME", "value": "/tmp"},
                    {"name": "OMP_NUM_THREADS", "value": "2"},
                    {"name": "FORECAST_GPU_LIMIT_BYTES", "value": str(gpu_bytes)}],
            "volumeMounts": [{"name": "inputs", "mountPath": "/base", "readOnly": True,
                              "subPath": "airaware/cache/weights/" + FLAMINGO_WEIGHTS},
                             {"name": "inputs", "mountPath": "/output", "subPath": "flamingo-baselines/" + run},
                             {"name": "tmp", "mountPath": "/tmp"}],
        }],
    }
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": "flamingo-base-" + run, "namespace": "training", "labels": labels},
            "spec": {"parallelism": 1, "completions": 1, "backoffLimit": 0, "activeDeadlineSeconds": deadline,
                     "template": {"metadata": {"labels": labels}, "spec": pod}}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True)
    p.add_argument("--node", required=True, help="GPU worker from the approved allocation")
    p.add_argument("--provider-id", required=True, help="Independent provider ID from that allocation")
    p.add_argument("--expected-deployment", required=True, help="Approved signed deployment SHA-256")
    p.add_argument("--deadline-seconds", required=True, type=int)
    p.add_argument("--gpu-limit-bytes", required=True, type=int)
    a = p.parse_args()
    identity = os.environ["NEBIUS_STAGING_OPERATOR_IDENTITY"]
    if identity in ("", "system:anonymous", "system:serviceaccount:training:episode-submit"):
        raise ValueError("Expected the independent placement reader identity")
    images, approval = current_release()
    if approval != a.expected_deployment:
        raise ValueError("Signed deployment differs from approval")
    job = render(images["flamingo-trainer"], a.run, a.node, a.deadline_seconds, a.gpu_limit_bytes)
    kube = kubectl_client_for_identity("training", identity, direct=True)
    node = json.loads(subprocess.check_output([*kube, "get", "node", a.node, "-o", "json"], timeout=15))
    if (node["metadata"]["name"] != a.node or node["metadata"].get("deletionTimestamp")
            or node["metadata"]["labels"].get("airaware.training/pool") != "training-gpu"
            or not a.provider_id or node["spec"]["providerID"] != a.provider_id
            or node["spec"].get("unschedulable") or node["status"]["allocatable"].get("nvidia.com/gpu") != "1"
            or not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"]["conditions"])):
        raise ValueError("Worker differs from approved Ready GPU allocation")
    raw = json.dumps(job, sort_keys=True, separators=(",", ":"))
    print(json.dumps({"schema": "flamingo-baseline-review-v1", "status": "pending-admission-and-funding",
                      "cluster_id": os.environ["NEBIUS_TRAINING_CLUSTER_ID"], "deployment_sha256": approval,
                      "provider_id": a.provider_id, "job_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                      "job_json": raw}, sort_keys=True))


if __name__ == "__main__":
    main()
