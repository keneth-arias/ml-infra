"""Renderizar el CronJob de actualización pública para bm o m4."""

import argparse
import hashlib
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--publisher-image", required=True)
parser.add_argument("--airaware-revision", required=True)
parser.add_argument("--capture-config", required=True)
parser.add_argument("--publish", action="store_true")
parser.add_argument("--target", choices=("bm", "orbstack"), default="bm")
parser.add_argument("--suspend", action="store_true")
args = parser.parse_args()
orb = args.target == "orbstack"
image_pin = r":cfg-[0-9a-f]{64}$" if orb else r"@sha256:[0-9a-f]{64}$"
if not re.search(image_pin, args.publisher_image) or len(args.airaware_revision) != 40:
    parser.error("Se requieren una imagen inmutable y una revisión Git completa")
namespace = "airaware-web-refresh"
collector = "docker.io/local/airaware-fresh-peak-collector@sha256:e8e3caa7645d74297af205c45aeb3af865cd24864fa9b2e96e95e7ae07a0ea64"
predictor = "docker.io/ml-infra/airaware-joint@sha256:14eb3ed9b9ca00c75acbc56991d0f1882dc8efc5306ccf5fa90366673f40b3be"
here = Path(__file__).resolve().parent
if orb:
    collector = predictor = args.publisher_image
code = {name: (here / name).read_text() for name in ("run.sh", "qwen.py", "gpu_worker.py", "publish.py")}
code_sha = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()
config = f"web-refresh-qwen-{code_sha[:12]}"


def container(name, image, phase):
    return {
        "name": name, "image": image, "imagePullPolicy": "Never",
        "command": ["/bin/sh", "/config/run.sh", phase],
        "env": [
            {"name": "JOB_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}}},
            {"name": "HOME", "value": "/tmp"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "PYTHONPATH", "value": "/config:/opt/fresh-peak:/opt/vendor:/opt/source/tools/timenet-smoke:/opt/forecast/src"},
            {"name": "OMP_NUM_THREADS", "value": "2"},
            {"name": "OPENBLAS_NUM_THREADS", "value": "1"},
            {"name": "CUDA_VISIBLE_DEVICES", "value": ""},
        ],
        "resources": {"requests": {"cpu": "300m", "memory": "256Mi", "ephemeral-storage": "100Mi"},
                      "limits": {"cpu": "2", "memory": "2Gi", "ephemeral-storage": "1Gi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "config", "mountPath": "/config", "readOnly": True},
                         {"name": "state", "mountPath": "/state"},
                         {"name": "tmp", "mountPath": "/tmp"}],
    }


capture = container("capture", collector, "capture")
capture["command"] = [
    "/bin/sh", "-ec",
    'exec /opt/python/bin/python3.13 -u /config/refresh.py capture "/state/runs/$JOB_NAME"',
]
capture["volumeMounts"][0]["name"] = "capture"
capture["env"].append({"name": "AIRAWARE_REVISION", "value": args.airaware_revision})
predict = container("forecast", predictor, "predict-mlx" if orb else "predict")
if orb:
    predict["volumeMounts"].append(
        {"name": "mlx-credentials", "mountPath": "/mlx-credentials", "readOnly": True},
    )
else:
    predict["env"] = [item for item in predict["env"] if item["name"] != "CUDA_VISIBLE_DEVICES"]
    predict["env"].append({"name": "HF_HUB_OFFLINE", "value": "1"})
    for limits in predict["resources"].values():
        limits.update(cpu="2", memory="12Gi", **{"nvidia.com/gpu": "1"})
    predict["volumeMounts"].append({"name": "model", "mountPath": "/model", "readOnly": True})
publish = container("publish", args.publisher_image, "publish")
publish["env"].append({"name": "PUBLISH", "value": str(args.publish).lower()})
pod = {
    "nodeSelector": {"kubernetes.io/hostname": args.target}, "restartPolicy": "Never",
    "automountServiceAccountToken": False, "enableServiceLinks": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                        "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
    "initContainers": [capture, predict], "containers": [publish],
    "volumes": [
        {"name": "config", "configMap": {"name": config}},
        {"name": "capture", "configMap": {"name": args.capture_config}},
        {"name": "state", "persistentVolumeClaim": {"claimName": "receipts"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
    ],
}
if orb:
    pod["volumes"].append({"name": "mlx-credentials", "secret": {
        "secretName": "web-refresh-mlx", "defaultMode": 288,
    }})
else:
    pod["runtimeClassName"] = "nvidia"
    pod["volumes"].append(
        {"name": "model", "persistentVolumeClaim": {"claimName": "qwen-08b-release", "readOnly": True}},
    )
if args.publish:
    publish["volumeMounts"].append({"name": "credentials", "mountPath": "/credentials", "readOnly": True})
    pod["volumes"].append(
        {"name": "credentials", "secret": {"secretName": "web-refresh-git", "defaultMode": 288}},
    )
objects = [
    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.36"}}},
    {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "receipts", "namespace": namespace},
     "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local-path" if orb else "airaware-hostpath-retain",
              "resources": {"requests": {"storage": "2Gi"}}}},
    {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
     "metadata": {"name": "qwen-08b-release", "namespace": namespace},
     "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "airaware-hostpath-retain",
              "resources": {"requests": {"storage": "3Gi"}}}},
    {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": config, "namespace": namespace},
     "immutable": True, "data": code},
    {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "airaware-hourly-forecast", "namespace": namespace},
     # The station charts can populate the current hour at different times.
     "spec": {"schedule": "15 * * * *" if orb else "0 * * * *",
              "timeZone": "America/Lima", "suspend": orb or args.suspend,
              "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 600,
              "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
              "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900,
                                       "ttlSecondsAfterFinished": 172800,
                                       "template": {"metadata": {"labels": {"app": "airaware-web-refresh"}}, "spec": pod}}}}},
]
if orb:
    objects = [item for item in objects if item["metadata"]["name"] != "qwen-08b-release"]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2))
