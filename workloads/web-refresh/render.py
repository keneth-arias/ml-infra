"""Renderizar el CronJob de actualización pública, fijado al MicroK8s local."""

import argparse
import json

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--publisher-image", required=True)
parser.add_argument("--airaware-revision", required=True)
args = parser.parse_args()
if "@sha256:" not in args.publisher_image or len(args.airaware_revision) != 40:
    parser.error("Se requieren una imagen inmutable y una revisión Git completa")
namespace = "airaware-web-refresh"
collector = "docker.io/local/airaware-fresh-peak-collector@sha256:e8e3caa7645d74297af205c45aeb3af865cd24864fa9b2e96e95e7ae07a0ea64"
predictor = "ghcr.io/keneth-arias/ml-infra-qwen@sha256:3378851e06efb96c8c975fc4cf2d2121e3a8efeb0ff03b392f5683e76258096b"


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
capture["env"] += [
    {"name": "NOT_BEFORE", "value": "2026-11-23T14:00:00Z"},
    {"name": "AIRAWARE_REVISION", "value": args.airaware_revision},
]
predict = container("forecast", predictor, "predict")
predict["volumeMounts"].append({"name": "model", "mountPath": "/model", "readOnly": True})
publish = container("publish", args.publisher_image, "publish")
publish["volumeMounts"].append({"name": "credentials", "mountPath": "/credentials", "readOnly": True})
pod = {
    "nodeSelector": {"kubernetes.io/hostname": "bm"}, "restartPolicy": "Never",
    "automountServiceAccountToken": False, "enableServiceLinks": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                        "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
    "initContainers": [capture, predict], "containers": [publish],
    "volumes": [
        {"name": "config", "configMap": {"name": "web-refresh-code"}},
        {"name": "model", "configMap": {"name": "web-refresh-cnn"}},
        {"name": "credentials", "secret": {"secretName": "web-refresh-git", "defaultMode": 288}},
        {"name": "state", "persistentVolumeClaim": {"claimName": "receipts"}},
        {"name": "tmp", "emptyDir": {"sizeLimit": "1Gi"}},
    ],
}
objects = [
    {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace, "labels": {
        "pod-security.kubernetes.io/enforce": "restricted",
        "pod-security.kubernetes.io/enforce-version": "v1.36"}}},
    {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "receipts", "namespace": namespace},
     "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "airaware-hostpath-retain",
              "resources": {"requests": {"storage": "2Gi"}}}},
    {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "airaware-hourly-forecast", "namespace": namespace},
     "spec": {"schedule": "0 * * * *", "timeZone": "America/Lima", "suspend": False,
              "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 600,
              "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
              "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900,
                                       "ttlSecondsAfterFinished": 172800,
                                       "template": {"metadata": {"labels": {"app": "airaware-web-refresh"}}, "spec": pod}}}}},
]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2))
