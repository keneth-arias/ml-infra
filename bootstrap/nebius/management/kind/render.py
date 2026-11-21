#!/usr/bin/env python3
"""Render the isolated fw13 backup in its own kind namespace. Initially suspended."""
import argparse
import json
import re

NS = "nebius-cleanup"
SA = "worker-expiry"


def resources(run, image, stage, *, armed=False, rehearsal=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]|[a-z0-9]", run):
        raise ValueError("Invalid run name")
    if not re.fullmatch(r"[a-z0-9./_-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected the qualified cleanup container digest")
    name = f"expiry-{run}"
    config = f"{name}-{stage}"
    if stage not in ("pilot", "support", "gpu"):
        raise ValueError("Unknown stage")
    spec = {
        "serviceAccountName": SA, "automountServiceAccountToken": False,
        "restartPolicy": "Never", "terminationGracePeriodSeconds": 30,
        "securityContext": {"runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000,
                            "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "cleanup", "image": image, "imagePullPolicy": "IfNotPresent",
            "args": [run, stage] + (["--rehearsal"] if rehearsal else []),
            "env": [{"name": "POD_NAME", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
                    {"name": "POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}],
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                          "limits": {"cpu": "1", "memory": "512Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "scratch", "mountPath": "/tmp"},
                             {"name": "observer", "mountPath": "/observer", "readOnly": True}],
        }],
        "volumes": [{"name": "scratch", "emptyDir": {"sizeLimit": "256Mi"}},
                    {"name": "observer", "projected": {"sources": [
                        {"serviceAccountToken": {"path": "token", "expirationSeconds": 900}},
                        {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}]}}],
    }
    if not rehearsal:
        spec["volumes"].extend([
            {"name": "scope", "configMap": {"name": config}},
            {"name": "identity", "secret": {"secretName": name + "-identity", "defaultMode": 0o440}},
        ])
        spec["containers"][0]["volumeMounts"].extend([
            {"name": key, "mountPath": "/" + key, "readOnly": True} for key in ("scope", "identity")
        ])
    namespace = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS, "labels": {
        "pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "v1.35"}}}
    account = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": SA, "namespace": NS},
               "automountServiceAccountToken": False}
    access = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
              "metadata": {"name": SA, "namespace": NS}, "rules": [
                  {"apiGroups": ["batch"], "resources": ["cronjobs", "jobs"], "verbs": ["get"]},
                  {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]}
    binding = {"apiVersion": access["apiVersion"], "kind": "RoleBinding", "metadata": dict(access["metadata"]),
               "roleRef": {"kind": "Role", "apiGroup": "rbac.authorization.k8s.io", "name": SA},
               "subjects": [{"kind": "ServiceAccount", "name": SA, "namespace": NS}]}
    cron = {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": name, "namespace": NS},
            "spec": {"schedule": "* * * * *", "timeZone": "Etc/UTC", "concurrencyPolicy": "Forbid",
                     "startingDeadlineSeconds": 45, "suspend": not armed, "successfulJobsHistoryLimit": 2,
                     "failedJobsHistoryLimit": 3, "jobTemplate": {"spec": {"backoffLimit": 0,
                     "activeDeadlineSeconds": 780, "ttlSecondsAfterFinished": 86400, "template": {"spec": spec}}}}}
    return [namespace, account, access, binding, cron]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run")
    p.add_argument("stage", choices=("pilot", "support", "gpu"))
    p.add_argument("image")
    p.add_argument("--armed", action="store_true")
    p.add_argument("--rehearsal", action="store_true", help="Schedule a credential-free ownership and connectivity test")
    a = p.parse_args()
    print(json.dumps({"apiVersion": "v1", "kind": "List",
                      "items": resources(a.run, a.image, a.stage, armed=a.armed, rehearsal=a.rehearsal)}, indent=2))
