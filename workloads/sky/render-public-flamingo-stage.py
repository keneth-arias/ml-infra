#!/usr/bin/env python3
"""Render an offline review packet for the original disk and public Flamingo base."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import runpy

import production_release as release
import public_flamingo_window as windows


HERE = Path(__file__).resolve().parent
WORKER = HERE / "public_flamingo_stage_worker.py"
HANDOFF = runpy.run_path(str(HERE / "training-storage-handoff.py"))
CLUSTER = "mk8scluster-e07hgffmypr4331sn2"
DEPLOY_COMMIT = "49dca1b5cc13934bcf3357870a20f8cf70decfda"
DEPLOYMENT_SHA = "1c92b48839f8fd1a660e641c85f26e1f9672344d2a20cf75e6fd4ca14839056f"
BINDING_SHA = "55c3fc877fd6e0cce829a66e5b20cb061a02bb60d3078d01411b24a7789ef653"
PROMOTION_SHA = "71e27185948aa31776654decf9edd55351a753d33985259ce1cd9d1555845b6c"
WEIGHTS = "7f8562992f1cf3852ced581f331247c6420fc90292d45453424c77f0a29c199f"
CPU_NODE = "computeinstance-e07bpk3dq3fyw99364"
CPU_UID = "b5579c4e-4b0a-4278-8b80-3aa8dd97f9bb"
CLAIM = "airaware-checkpoints"


# Read the reviewed bytes once. Execute exactly the bytes that passed the hash check.
BOOTSTRAP = """from hashlib import sha256
from pathlib import Path
import sys
worker_path, objects_path, worker_sha, objects_sha = sys.argv[1:]
worker = Path(worker_path).read_bytes()
if sha256(worker).hexdigest() != worker_sha or sha256(Path(objects_path).read_bytes()).hexdigest() != objects_sha:
    raise SystemExit("Public staging program or object manifest changed")
sys.argv = [worker_path, objects_sha]
exec(compile(worker, worker_path, "exec"), {"__name__": "__main__", "__file__": worker_path})
"""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def public_objects(promotion):
    raw = promotion.read_bytes()
    if sha256(raw).hexdigest() != PROMOTION_SHA:
        raise ValueError("Public promotion receipt differs from the pinned bytes")
    rows = [dict(row, name=row["name"].removeprefix("base/"))
            for row in json.loads(raw)["files"] if row["name"].startswith("base/")]
    return canonical(sorted(rows, key=lambda row: row["name"]))


def stage_job(run, inventory_secret, configmap, image, worker_sha, objects_sha, window=None):
    labels = {"airaware.training/phase": "public-flamingo-stage", "airaware.training/run": run}
    container = {
        "name": "stage", "image": image, "imagePullPolicy": "IfNotPresent",
        "command": ["/usr/local/bin/python", "-I", "-B", "-c", BOOTSTRAP,
                    "/program/public_flamingo_stage_worker.py", "/program/objects.json",
                    worker_sha, objects_sha],
        "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "1", "memory": "128Mi"},
                      "limits": {"cpu": "2", "memory": "512Mi"}},
        "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
        "volumeMounts": [
            {"name": "claim", "mountPath": "/base", "subPath": "airaware/cache/weights/" + WEIGHTS},
            {"name": "claim", "mountPath": "/output", "subPath": "flamingo-baselines/" + run},
            {"name": "program", "mountPath": "/program", "readOnly": True},
            {"name": "inventory", "mountPath": "/source", "readOnly": True},
        ],
    }
    pod = {
        "serviceAccountName": "episode-input-stager", "automountServiceAccountToken": False,
        "priority": 0, "preemptionPolicy": "PreemptLowerPriority",
        "tolerations": [{"key": "node.kubernetes.io/" + condition, "operator": "Exists", "effect": "NoExecute",
                         "tolerationSeconds": 300} for condition in ("not-ready", "unreachable")],
        "restartPolicy": "Never", "enableServiceLinks": False, "terminationGracePeriodSeconds": 30,
        "dnsPolicy": "ClusterFirst", "dnsConfig": {"options": [{"name": "ndots", "value": "1"}]},
        "schedulerName": "default-scheduler", "imagePullSecrets": [{"name": "ghcr-pull"}],
        "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "nodeSelector": {"airaware.training/pool": "training-cpu"},
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
            "nodeSelectorTerms": [{"matchFields": [
                {"key": "metadata.name", "operator": "In", "values": [window.cpu_node if window else CPU_NODE]}]}]}}},
        "containers": [container],
        "volumes": [
            {"name": "claim", "persistentVolumeClaim": {"claimName": CLAIM}},
            {"name": "program", "configMap": {"name": configmap, "defaultMode": 0o444}},
            {"name": "inventory", "secret": {"secretName": inventory_secret, "defaultMode": 0o444,
                                             "items": [{"key": "inventory.json", "path": "inventory.json"}]}},
        ],
    }
    meta = {"namespace": "training", "name": "stage-" + run, "labels": labels}
    if window:
        meta["annotations"] = {"airaware.training/stage-window-sha256": window.sha256}
        pod["nodeSelector"][windows.eligibility_label(run)] = window.cpu_node_uid
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 1800, "parallelism": 1,
                     "completions": 1, "podReplacementPolicy": "Failed",
                     "template": {"metadata": {"labels": labels}, "spec": pod}}}


def build_packet(promotion, binding, expected_deployment, run, inventory_secret, window=None):
    windows.validate_window_run(window, run)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", run):
        raise ValueError("Invalid public baseline run")
    if inventory_secret != f"stage-{run}-inventory":
        raise ValueError("Expected the inventory Secret reserved for this run")
    commit = window.deployment_commit if window else DEPLOY_COMMIT
    binding_sha = window.binding_sha256 if window else BINDING_SHA
    cluster = window.cluster_id if window else CLUSTER
    images, fingerprint = release.verify_release(release.TRUST, cluster, commit)
    if fingerprint != expected_deployment or fingerprint != (window.deployment_sha256 if window else DEPLOYMENT_SHA):
        raise ValueError("Signed deployment differs from the enrolled original and independent pin")
    project, (pvc_uid, pv_uid, disk) = HANDOFF["approved_binding"](binding, binding_sha, cluster)
    # Read only the PV name from the identical, pinned receipt.
    bound_raw = binding.read_bytes()
    if sha256(bound_raw).hexdigest() != binding_sha:
        raise ValueError("Binding receipt changed during rendering")
    training, = (item for item in json.loads(bound_raw)["bindings"]
                 if (item["namespace"], item["claim"]) == ("training", CLAIM))
    objects = public_objects(promotion)
    worker = WORKER.read_bytes()
    worker_sha = sha256(worker).hexdigest()
    objects_sha = sha256(objects).hexdigest()
    configmap_name = f"stage-{run}-program"
    configmap = {
        "apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
        "metadata": {"namespace": "training", "name": configmap_name},
        "data": {"public_flamingo_stage_worker.py": worker.decode("utf-8"), "objects.json": objects.decode()},
    }
    image = images["preflight"]
    job = stage_job(run, inventory_secret, configmap_name, image, worker_sha, objects_sha, window)
    extra = {}
    if window:
        if (project != window.project_id or image != window.preflight_image
                or objects_sha != window.objects_sha256 or PROMOTION_SHA != window.promotion_sha256
                or WEIGHTS != window.weights_manifest_sha256):
            raise ValueError("Public stage inputs differ from the signed window")
        configmap["metadata"]["annotations"] = dict(job["metadata"]["annotations"])
        extra = {"window_sha256": window.sha256, "scope_sha256": window.scope_sha256,
                 "preflight_linux_amd64": window.preflight_platform_image}
    return {
        **extra,
        "schema": "public-flamingo-stage-review-v1", "run": run, "cluster_id": cluster,
        "project_id": project, "deployment_commit": commit, "deployment_sha256": fingerprint,
        "image": image, "promotion_sha256": PROMOTION_SHA, "weights_manifest_sha256": WEIGHTS,
        "placement": {"name": window.cpu_node if window else CPU_NODE,
                      "uid": window.cpu_node_uid if window else CPU_UID,
                      "provider_id": window.cpu_provider_id if window else "nebius://" + CPU_NODE},
        "binding_sha256": binding_sha,
        "binding": {"namespace": "training", "claim": CLAIM, "pv": training["pv"], "pv_uid": pv_uid,
                    "pvc_uid": pvc_uid, "provider_disk_id": disk},
        "inventory_secret": inventory_secret, "worker_sha256": worker_sha, "objects_sha256": objects_sha,
        "configmap": configmap, "configmap_sha256": sha256(canonical(configmap)).hexdigest(),
        "job": job, "job_sha256": sha256(canonical(job)).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promotion", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--expected-deployment", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--inventory-secret", required=True)
    windows.add_window_args(parser)
    args = parser.parse_args()
    window = windows.load_window(args, args.run, live=False)
    print(json.dumps(build_packet(args.promotion, args.binding, args.expected_deployment,
                                  args.run, args.inventory_secret, window), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
