#!/usr/bin/env python3
"""Qualify a single funded CPU worker using the scoped native Job submitter."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sky"))
from production_target import kubectl_client

IMAGE = "docker.io/library/python@sha256:b701198cb708b68d9413ca67d6311bd97c9389a2c5fe4da0511fc652642366cd"
NS = "sky-cpu"
PROBE = """\
import ipaddress, json, os, socket
from pathlib import Path

status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
if (os.geteuid() != 65532 or int(status['CapEff'], 16) != 0 or int(status['NoNewPrivs']) != 1
    or int(status['Seccomp']) != 2 or Path('/var/run/secrets/kubernetes.io/serviceaccount/token').exists()):
    raise RuntimeError('Expected a token-free Restricted process')
service = str(ipaddress.ip_address(os.environ['KUBERNETES_SERVICE_HOST']))
addresses = socket.getaddrinfo('kubernetes.default.svc.cluster.local', 443, type=socket.SOCK_STREAM)
if not any(str(ipaddress.ip_address(addr[4][0])) == service for addr in addresses):
    raise RuntimeError('Cluster DNS did not resolve the Kubernetes Service IP')
try:
    with socket.create_connection((service, 443), timeout=3):
        raise RuntimeError('Pilot reached the Kubernetes API directly')
except (TimeoutError, PermissionError):
    pass
print(json.dumps({'pod_uid': os.environ['PILOT_POD_UID'], 'restricted': True,
                  'dns': 'resolved', 'api_tcp': 'blocked'}), flush=True)
"""


def job(run_id, node):
    if not re.fullmatch("[a-z][a-z0-9-]{1,23}[a-z0-9]", run_id):
        raise ValueError("Use a unique 3-25 character run ID")
    if len(node) > 253 or not all(
        re.fullmatch("[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", part)
        for part in node.split(".")
    ):
        raise ValueError("Expected the verified CPU worker's Kubernetes node name")
    resources = {"cpu": "1", "memory": "2Gi"}
    pod = {
        "restartPolicy": "Never",
        "activeDeadlineSeconds": 180,
        "automountServiceAccountToken": False,
        "serviceAccountName": "skypilot-service-account",
        "nodeSelector": {"airaware.training/pool": "training-cpu"},
        "affinity": {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchFields": [
                                {
                                    "key": "metadata.name",
                                    "operator": "In",
                                    "values": [node],
                                }
                            ]
                        }
                    ]
                }
            }
        },
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "probe",
                "image": IMAGE,
                "imagePullPolicy": "Always",
                "command": ["python3", "-I", "-c", PROBE],
                "env": [
                    {
                        "name": "PILOT_POD_UID",
                        "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}},
                    }
                ],
                "resources": {"requests": resources, "limits": resources},
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        ],
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "cpu-" + run_id, "namespace": NS},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 180,
            "ttlSecondsAfterFinished": 900,
            "template": {
                "metadata": {"labels": {"airaware.training/run": run_id}},
                "spec": pod,
            },
        },
    }


def kubectl(client, *args, body=None):
    return subprocess.check_output(
        [*client, "--request-timeout=15s", *args],
        input=None if body is None else json.dumps(body),
        text=True,
    )


def owned_pods(client, uid):
    return json.loads(
        kubectl(
            client,
            "get",
            "pods",
            "-l",
            "batch.kubernetes.io/controller-uid=" + uid,
            "-o",
            "json",
        )
    )["items"]


def verify(pod, logs, name, uid, node):
    metadata, spec, status = pod["metadata"], pod["spec"], pod["status"]
    owners = [o for o in metadata["ownerReferences"] if o.get("controller")]
    if (
        len(owners) != 1
        or owners[0]["uid"] != uid
        or owners[0]["name"] != name
        or owners[0]["kind"] != "Job"
        or spec["nodeName"] != node
        or metadata["namespace"] != NS
        or status["phase"] != "Succeeded"
        or [c["image"] for c in spec["containers"]] != [IMAGE]
    ):
        raise ValueError(
            "Pod ownership, placement, phase or admitted image differs from the pilot"
        )
    (container,) = status["containerStatuses"]
    if (
        container["name"] != "probe"
        or container["restartCount"] != 0
        or container["state"]["terminated"]["exitCode"] != 0
        or not container["imageID"]
    ):
        raise ValueError("The pilot requires one successful container execution")
    if json.loads(logs) != {
        "pod_uid": metadata["uid"],
        "restricted": True,
        "dns": "resolved",
        "api_tcp": "blocked",
    }:
        raise ValueError(
            "Missing runtime, DNS or network isolation proof for the observed Pod UID"
        )
    return {
        "namespace": NS,
        "job": name,
        "job_uid": uid,
        "pod_uid": metadata["uid"],
        "node": node,
        "image": IMAGE,
        "image_id": container["imageID"],
        "probe": json.loads(logs),
    }


def remove(client, name, uid):
    options = {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "propagationPolicy": "Foreground",
        "preconditions": {"uid": uid},
    }
    deadline = time.monotonic() + 60
    last_error = None
    delete_again = True
    accepted = False
    while time.monotonic() < deadline:
        if delete_again:
            delete_again = False
            try:
                kubectl(
                    client,
                    "delete",
                    "--raw",
                    f"/apis/batch/v1/namespaces/{NS}/jobs/{name}",
                    "-f",
                    "-",
                    body=options,
                )
                accepted = True
            except (subprocess.SubprocessError, OSError) as error:
                last_error = error
        try:
            current = kubectl(
                client,
                "get",
                "job",
                name,
                "--ignore-not-found",
                "-o",
                "jsonpath={.metadata.uid}",
            )
            if current == uid:
                delete_again = not accepted
            elif not owned_pods(client, uid):
                return
        except (subprocess.SubprocessError, OSError) as error:
            last_error = error
        time.sleep(2)
    raise TimeoutError(f"Reconcile Job {name} UID {uid} and its Pods") from last_error


def qualify(client, manifest, node, execute):
    name = manifest["metadata"]["name"]
    # Check admission before sending a persistent create. Each run creates its own Job.
    kubectl(
        client,
        "create",
        "--dry-run=server",
        "--validate=strict",
        "-f",
        "-",
        "-o",
        "name",
        body=manifest,
    )
    if not execute:
        return {"namespace": NS, "job": name, "admission": "passed", "executed": False}
    result = json.loads(
        kubectl(client, "create", "-f", "-", "-o", "json", body=manifest)
    )
    uid = str(uuid.UUID(result["metadata"]["uid"]))
    print(f"Created {NS}/{name} UID {uid}", file=sys.stderr, flush=True)
    try:
        # A watch can outlast an individual Kubernetes HTTP request.
        subprocess.run(
            [
                *client,
                "wait",
                "--for=condition=complete",
                "--timeout=210s",
                "job/" + name,
            ],
            check=True,
            timeout=225,
            stdout=subprocess.DEVNULL,
        )
        (pod,) = owned_pods(client, uid)
        logs = kubectl(client, "logs", pod["metadata"]["name"], "-c", "probe")
        receipt = verify(pod, logs, name, uid, node)
    except BaseException as error:
        try:
            remove(client, name, uid)
        except Exception as cleanup:
            error.add_note(f"Cleanup of {name} UID {uid} also failed: {cleanup}")
        raise
    remove(client, name, uid)
    return dict(receipt, executed=True, cleanup="verified")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument(
        "--node",
        required=True,
        help="Ready node name verified against the funded worker",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--render", action="store_true", help="Render the Job offline")
    modes.add_argument(
        "--execute",
        action="store_true",
        help="Run on the funded worker and collect evidence",
    )
    args = parser.parse_args()
    manifest = job(args.run_id, args.node)
    if args.render:
        result = manifest
    else:
        client = kubectl_client(NS, "cpu-submit", direct=True)
        result = qualify(client, manifest, args.node, args.execute)
        result["cluster_id"] = os.environ["NEBIUS_TRAINING_CLUSTER_ID"]
    print(json.dumps(result, sort_keys=True))
