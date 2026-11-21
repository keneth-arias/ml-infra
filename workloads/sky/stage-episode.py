#!/usr/bin/env python3
"""Render a CPU Job that stages immutable, versioned inputs before GPU allocation."""

import argparse
import json
import re

p = argparse.ArgumentParser(description=__doc__)
p.add_argument(
    "--image", required=True,
    help="Digest of the input-staging image or a derived preflight image providing /usr/local/bin/python",
)
p.add_argument("--inventory-secret", required=True)
p.add_argument("--source-prefix", required=True)
p.add_argument("--weights-sha256", required=True)
p.add_argument("--bundle-sha256", required=True)
p.add_argument("--validation-sha256", help="Pinned inner-validation manifest for a direct paired fit")
p.add_argument("--phase", choices=["fit", "infer"], required=True)
p.add_argument("--head", choices=["direct", "episode", "quantile"], required=True)
a = p.parse_args()
if not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", a.image) or not re.fullmatch(
    "[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?", a.inventory_secret
):
    p.error("Invalid immutable image or inventory Secret name")
if not all(
    re.fullmatch("[0-9a-f]{64}", v) for v in [a.bundle_sha256, a.weights_sha256]
):
    p.error("Expected immutable input digests")
if not re.fullmatch(
    r"https://storage\.eu-south1\.nebius\.cloud/[a-z0-9.-]+/(?:[A-Za-z0-9_-]+/)+",
    a.source_prefix,
):
    p.error("Expected a versioned source bucket prefix")
if a.validation_sha256 and (
    not re.fullmatch("[0-9a-f]{64}", a.validation_sha256)
    or a.validation_sha256 == a.bundle_sha256 or (a.phase, a.head) != ("fit", "direct")
):
    p.error("Inner validation requires a distinct digest and a direct fit")
spec = {
    "restartPolicy": "Never",
    "serviceAccountName": "episode-input-stager",
    "automountServiceAccountToken": False,
    "dnsConfig": {"options": [{"name": "ndots", "value": "1"}]},
    "nodeSelector": {"airaware.training/pool": "training-cpu"},
    "imagePullSecrets": [{"name": "ghcr-pull"}],
    "securityContext": {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "fsGroupChangePolicy": "OnRootMismatch",
        "seccompProfile": {"type": "RuntimeDefault"},
    },
    "containers": [
        {
            "name": "stage",
            "image": a.image,
            "command": [
                "/usr/local/bin/python",
                "/opt/ml-infra/stage_episode_inputs.py",
                "--cache",
                "/inputs",
                "--inventory",
                "/source/inventory.json",
                "--source-prefix",
                a.source_prefix,
                "--weights-sha256",
                a.weights_sha256,
                "--bundle-sha256",
                a.bundle_sha256,
                *(["--validation-sha256", a.validation_sha256] if a.validation_sha256 else []),
                "--phase",
                a.phase,
                "--head",
                a.head,
            ],
            "securityContext": {
                "readOnlyRootFilesystem": True,
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            },
            "resources": {
                "requests": {"cpu": "1", "memory": "128Mi"},
                "limits": {"cpu": "2", "memory": "512Mi"},
            },
            "volumeMounts": [
                {"name": "inputs", "mountPath": "/inputs", "subPath": "airaware/cache"},
                {"name": "source", "mountPath": "/source", "readOnly": True},
            ],
        }
    ],
    "volumes": [
        {
            "name": "inputs",
            "persistentVolumeClaim": {"claimName": "airaware-checkpoints"},
        },
        {
            "name": "source",
            "secret": {
                "secretName": a.inventory_secret,
                "items": [{"key": "inventory.json", "path": "inventory.json"}],
            },
        },
    ],
}
print(
    json.dumps(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"generateName": "stage-episode-", "namespace": "training"},
            "spec": {
                "activeDeadlineSeconds": 1800,
                "ttlSecondsAfterFinished": 86400,
                "backoffLimit": 0,
                "template": {"spec": spec},
            },
        },
        indent=2,
    )
)
