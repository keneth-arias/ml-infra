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
predictor = "localhost/ml-infra/gemma-trainer@sha256:1ede4194d1d0a10b3aa551fc6971f169284d30bb17a2376858b77d72575cda9b"


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
            {"name": "HF_HUB_OFFLINE", "value": "1"},
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
predict["volumeMounts"].extend([
    {"name": "model", "mountPath": "/model/SHA256SUMS", "subPath": "SHA256SUMS", "readOnly": True},
    {"name": "model", "mountPath": "/model/weights", "subPath": "weights", "readOnly": True},
    {"name": "model", "mountPath": "/model/step-00004500", "subPath": "step-00004500", "readOnly": True},
])
for resources in predict["resources"].values():
    resources.update({"cpu": "2", "memory": "12Gi", "nvidia.com/gpu": "1"})
correct = container("correction", "docker.io/local/airaware-scorer@sha256:95ed7b9099f980dec1c3fdd1c1760e17f5b38e4d2540f62adb50e7821663c14e", "correct")
correct["volumeMounts"].extend([
    {"name": "model", "mountPath": "/model/correction.json", "subPath": "calibration/correction.json", "readOnly": True},
    {"name": "model", "mountPath": "/release/tools/timenet-smoke", "subPath": "calibration/tools/timenet-smoke", "readOnly": True},
])
correct["env"] = [entry for entry in correct["env"] if entry["name"] != "PYTHONPATH"]
publish = container("publish", args.publisher_image, "publish")
publish["volumeMounts"].append({"name": "credentials", "mountPath": "/credentials", "readOnly": True})
pod = {
    "nodeSelector": {"kubernetes.io/hostname": "bm"}, "restartPolicy": "Never",
    "automountServiceAccountToken": False, "enableServiceLinks": False,
    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                        "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
    "initContainers": [capture, predict, correct], "containers": [publish],
    "volumes": [
        {"name": "config", "configMap": {"name": "web-refresh-gemma-code"}},
        {"name": "model", "persistentVolumeClaim": {"claimName": "gemma-1b-release", "readOnly": True}},
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
    {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": "gemma-1b-release", "namespace": namespace},
     "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "airaware-hostpath-retain",
              "resources": {"requests": {"storage": "6Gi"}}}},
    {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "airaware-hourly-forecast", "namespace": namespace},
     "spec": {"schedule": "0 * * * *", "timeZone": "America/Lima", "suspend": False,
              "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 600,
              "successfulJobsHistoryLimit": 3, "failedJobsHistoryLimit": 3,
              "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 900,
                                       "ttlSecondsAfterFinished": 172800,
                                       "template": {"metadata": {"labels": {"app": "airaware-web-refresh"}}, "spec": pod}}}}},
]
print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects}, indent=2))
