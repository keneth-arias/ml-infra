#!/usr/bin/env python3
"""Issue and revoke attended, API-audience credentials for the CPU pilot."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "workloads/sky"))
from private_api import api_host, pinned_kubeconfig_ca

NAMESPACE = "sky-cpu"
ACCOUNTS = ("sky-backend", "cpu-submit")


def kubectl(client, *args, payload=None):
    return subprocess.check_output(
        [*client, *args],
        input=None if payload is None else json.dumps(payload),
        text=True,
    )


def write_private(path, payload):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def revoke(client, receipt):
    name, uid = receipt["marker"], receipt["uid"]
    if (
        receipt["account"] not in ACCOUNTS
        or not re.fullmatch(r"cpu-session-[0-9a-f]{32}", name)
        or str(uuid.UUID(uid)) != uid
    ):
        raise ValueError("Invalid CPU session marker or UID")

    def live_uid():
        return kubectl(
            client,
            "-n",
            NAMESPACE,
            "get",
            "secret",
            name,
            "--ignore-not-found",
            "-o",
            "jsonpath={.metadata.uid}",
        )

    if live_uid() != uid:
        return
    # A replacement object with the same name must survive a stale closeout.
    options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
    try:
        kubectl(
            client,
            "delete",
            "--raw",
            f"/api/v1/namespaces/{NAMESPACE}/secrets/{name}",
            "-f",
            "-",
            payload=options,
        )
    except subprocess.CalledProcessError:
        if live_uid() == uid:
            raise
        return
    deadline = time.monotonic() + 30
    while live_uid() == uid:
        if time.monotonic() >= deadline:
            raise ValueError("Session marker is still present; keep the receipt and retry revocation")
        time.sleep(1)


def issue(client, ca, target, session, account, seconds):
    session.mkdir(mode=0o700)
    name = "cpu-session-" + uuid.uuid4().hex
    marker = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "immutable": True,
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"airaware.training/session": account},
        },
    }
    # Preserve the generated name even when the create response is lost.
    write_private(session / "intent.json", {**target, "account": account, "marker": name})
    uid = kubectl(client, "create", "-f", "-", "-o", "jsonpath={.metadata.uid}", payload=marker)
    receipt = {**target, "account": account, "marker": name, "uid": uid}
    try:
        write_private(session / "marker.json", receipt)
        token_request = json.loads(
            kubectl(
                client,
                "-n",
                NAMESPACE,
                "create",
                "token",
                account,
                f"--duration={seconds}s",
                "--bound-object-kind=Secret",
                f"--bound-object-name={name}",
                f"--bound-object-uid={uid}",
                "-o",
                "json",
            )
        )
        token = token_request["status"]
        now = datetime.now(timezone.utc)
        expires = datetime.fromisoformat(token["expirationTimestamp"].replace("Z", "+00:00"))
        if not now + timedelta(minutes=1) < expires <= now + timedelta(seconds=seconds + 60):
            raise ValueError("Unexpected granted token lifetime")
        url = f"https://{api_host(target['cluster_id'], target['endpoint'])}:443"
        config = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "prod-nebius",
            "clusters": [
                {
                    "name": "prod-nebius",
                    "cluster": {"server": url, "certificate-authority-data": base64.b64encode(ca).decode()},
                }
            ],
            "contexts": [
                {
                    "name": "prod-nebius",
                    "context": {"cluster": "prod-nebius", "namespace": NAMESPACE, "user": account},
                }
            ],
            "users": [{"name": account, "user": {"token": token["token"]}}],
        }
        pending = session / "pending"
        write_private(pending, config)
        identity = kubectl(
            ["kubectl", "--kubeconfig", str(pending), "--context", "prod-nebius", "--request-timeout=10s"],
            "auth",
            "whoami",
            "-o",
            "jsonpath={.status.userInfo.username}",
        )
        if identity != f"system:serviceaccount:{NAMESPACE}:{account}":
            raise ValueError("Managed API returned an unexpected identity")
        receipt.update(
            identity=identity,
            expires_at=token["expirationTimestamp"],
            issued_at=now.isoformat(),
            audiences=token_request["spec"]["audiences"],
        )
        write_private(session / "issued.json", receipt)
        pending.rename(session / "kubeconfig")
    except BaseException:
        (session / "pending").unlink(missing_ok=True)
        revoke(client, receipt)
        raise
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--operator-config", required=True, type=Path)
    p.add_argument("--cluster-id", required=True)
    p.add_argument("--ca-bundle-sha256", required=True)
    p.add_argument("--endpoint", choices=("private", "public"), required=True)
    commands = p.add_subparsers(dest="action", required=True)
    new = commands.add_parser("issue")
    new.add_argument("--account", choices=ACCOUNTS, required=True)
    new.add_argument("--seconds", type=int, default=1800)
    for command in (new, commands.add_parser("revoke")):
        command.add_argument("session", type=Path)
    a = p.parse_args()
    os.umask(0o077)
    directory = a.session.absolute()
    if directory.parent.stat().st_uid != os.getuid() or directory.parent.stat().st_mode & 0o077:
        p.error("Use an operator-owned private parent directory")
    target = {"endpoint": a.endpoint, "cluster_id": a.cluster_id, "ca_bundle_sha256": a.ca_bundle_sha256}
    client = [
        "kubectl",
        "--kubeconfig",
        str(a.operator_config),
        "--context",
        "prod-nebius",
        "--request-timeout=15s",
    ]
    ca = pinned_kubeconfig_ca(client, a.cluster_id, a.ca_bundle_sha256, endpoint=a.endpoint, direct=True)
    if a.action == "issue":
        if not 600 <= a.seconds <= 28800:
            p.error("Expected a session between 10 minutes and 8 hours")
        print(json.dumps(issue(client, ca, target, directory, a.account, a.seconds), sort_keys=True))
    else:
        receipt = json.loads((directory / "marker.json").read_bytes())
        if any(receipt[k] != v for k, v in target.items()):
            p.error("Session belongs to another target")
        revoke(client, receipt)
        for credential in ("pending", "kubeconfig"):
            (directory / credential).unlink(missing_ok=True)
        print("Revoked", receipt["account"], "session", receipt["marker"])


if __name__ == "__main__":
    main()
