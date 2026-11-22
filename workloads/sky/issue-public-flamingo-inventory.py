#!/usr/bin/env python3
"""Issue the approved immutable source inventory with an enrolled version-only reader."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.request import build_opener, ProxyHandler, Request
from uuid import UUID

from botocore.auth import S3SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from public_flamingo_stage_guard import verify_guard
from public_flamingo_stage_worker import HOST, NoRedirect, validate_query


HERE = Path(__file__).resolve().parent
RENDER = runpy.run_path(str(HERE / "render-public-flamingo-stage.py"))["build_packet"]
SCOPE_SHA = "a82b7ef7bd96295b9ce33fa10d629be2e19b1c9f8dd0b55f0c513921804be7f6"
CA_SHA = "122a800e233ecae05a71701c057a0f77d786b5c7e357e9cb93b6dfff0b5b301e"
KUBECTL = "/usr/local/bin/kubectl"
NO_PIN = object()


def read_private(path, pin=NO_PIN):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Expected a private owner-held file")
        raw = stream.read(2 ** 20 + 1)
    if len(raw) > 2 ** 20 or (pin is not NO_PIN and sha256(raw).hexdigest() != pin):
        raise ValueError("Source evidence size or independent digest mismatch")
    return json.loads(raw)


def timestamp(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return dt


def check_grant(grant, credential, packet, approval):
    # Sign directly with these bytes. Never resolve a boto profile, role or instance credential.
    ambient = {"AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
               "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
               "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
               "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"}
    if ambient.intersection(os.environ):
        raise ValueError("Ambient AWS credentials or profiles present")
    fields = {"access_key_id", "secret_access_key"}
    if set(credential) not in (fields, fields | {"session_token"}) or not all(
        isinstance(value, str) and value.strip() for value in credential.values()
    ):
        raise ValueError("Expected explicit reader credentials")
    if (set(grant) != {"schema", "principal", "access_key_id_sha256", "project_id", "objects_sha256",
                       "permissions", "exact_versions_only", "expires_at"}
            or grant["schema"] != "public-flamingo-version-reader-grant-v1"
            or not re.fullmatch(r"serviceaccount-[a-z0-9]+", grant["principal"])
            or grant["access_key_id_sha256"] != sha256(credential["access_key_id"].encode()).hexdigest()
            or grant["project_id"] != packet["project_id"] or grant["objects_sha256"] != packet["objects_sha256"]
            or grant["permissions"] != ["s3:GetObjectVersion"] or grant["exact_versions_only"] is not True):
        raise ValueError("Missing effective grant for exactly the promoted public versions")
    operators = [approval[name] for name in ("source_operator", "storage_operator", "staging_operator")]
    if (len(set(operators)) != 3 or not all(operators) or grant["principal"] in operators
            or grant["principal"] == "serviceaccount-e07ahpx591vnjdv77y"
            or approval["source_operator"] in {"system:anonymous", "serviceaccount-e07ahpx591vnjdv77y"}
            or approval["source_operator"] in {f"system:serviceaccount:training:{name}" for name in
                                               ("episode-input-stager", "episode-submit", "episode-worker", "default")}):
        raise ValueError("Expected independent reader, inventory issuer, storage and staging identities")


def presign(objects, credential, lifetime, latest):
    if type(lifetime) is not int or not 1 <= lifetime <= 1800:
        raise ValueError("Source URL lifetime must be within 1–1800 seconds")
    creds = Credentials(credential["access_key_id"], credential["secret_access_key"], credential.get("session_token"))
    signer = S3SigV4QueryAuth(creds, "s3", "eu-south1", expires=lifetime)
    inventory = []
    for row in objects:
        if datetime.now(timezone.utc) + timedelta(seconds=lifetime) >= latest:
            raise ValueError("URL would exceed the grant, original cutoff or owner expiry")
        request = AWSRequest(method="GET", url=f"https://{HOST}/{row['bucket']}/{row['key']}",
                             params={"versionId": row["version_id"]})
        signer.add_auth(request)
        url = request.prepare().url
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        validate_query(query, row["version_id"])
        signed_at = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if signed_at + timedelta(seconds=lifetime) >= latest:
            raise ValueError("Issued URL exceeds the approved window")
        inventory.append({**{field: row[field] for field in ("name", "size", "sha256", "version_id")}, "url": url})
    return inventory


def probe_versions(inventory):
    # Prove that the enrolled credential currently reads each selected version. Breadth of
    # its effective privileges is independently attested in the approved grant.
    client = build_opener(ProxyHandler({}), NoRedirect())
    for row in inventory:
        request = Request(row["url"], headers={"Range": "bytes=0-0"})
        with client.open(request, timeout=10) as response:
            if (response.status != 206 or response.headers.get("x-amz-version-id") != row["version_id"]
                    or response.headers.get("Content-Range") != f"bytes 0-0/{row['size']}"
                    or len(response.read(2)) != 1):
                raise ValueError("Source reader could not verify an exact promoted version")


def source_client(approval, cluster):
    if (os.environ["NEBIUS_TRAINING_CLUSTER_ID"] != cluster or os.environ["NEBIUS_CA_BUNDLE_SHA256"] != CA_SHA):
        raise ValueError("Inventory issuer requires the enrolled managed target and CA")
    mode = Path(KUBECTL).stat()
    if mode.st_uid != 0 or not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o022:
        raise ValueError("Expected the trusted host kubectl")
    # The production verifier invokes 'kubectl' internally. Resolve it exclusively from the
    # trusted host directory, suppress its diagnostics and keep its configuration private.
    verify = """import json,sys
sys.path.insert(0, sys.argv[1])
from production_target import kubectl_client_for_identity
print(json.dumps(kubectl_client_for_identity("training", sys.argv[2], direct=True)))
"""
    verified = subprocess.run([sys.executable, "-I", "-B", "-c", verify, str(HERE), approval["source_operator"]],
                              env={**os.environ, "PATH": "/usr/local/bin:/usr/bin:/bin"},
                              text=True, capture_output=True, timeout=45)
    if verified.returncode:
        raise ValueError("Source-owner identity or managed target verification failed")
    argv = json.loads(verified.stdout)
    if argv[0] != "kubectl":
        raise ValueError("Expected installed management kubectl")
    return [KUBECTL, *argv[1:]]


def create_secret(kube, name, inventory):
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
              "metadata": {"namespace": "training", "name": name},
              "stringData": {"inventory.json": json.dumps({"base": inventory}, separators=(",", ":"))}}
    try:
        result = subprocess.run([*kube, "--request-timeout=30s", "create", "-f", "-", "-o",
                                 'jsonpath={.metadata.uid}{" "}{.metadata.resourceVersion}'],
                                input=json.dumps(secret), text=True, capture_output=True, timeout=35)
        if result.returncode == 0:
            uid, version = result.stdout.strip().split()
            if str(UUID(uid)) == uid and version:
                return {"namespace": "training", "name": name, "uid": uid, "resourceVersion": version}
    except (ValueError, subprocess.TimeoutExpired, OSError):
        pass
    raise RuntimeError(f"Secret creation unconfirmed; inspect training/{name} by identity before any retry")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("packet", "promotion", "binding", "scope", "approval", "reader-grant", "reader-credentials", "consumer-check"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--approved-sha256", required=True)
    args = p.parse_args()
    approval = read_private(args.approval, args.approved_sha256)
    if approval["purpose"] != "issue-public-flamingo-inventory":
        raise ValueError("Expected independent source-owner approval")
    packet = RENDER(args.promotion, args.binding, approval["deployment_sha256"], approval["run"],
                    approval["inventory_secret"])
    if packet != read_private(args.packet, approval["packet_file_sha256"]):
        raise ValueError("Source-owner approval differs from the reproduced packet")
    scope = read_private(args.scope, SCOPE_SHA)
    if (scope["cluster_id"], scope["project_id"]) != (packet["cluster_id"], packet["project_id"]):
        raise ValueError("Original allocation differs from the reviewed target")
    grant = read_private(args.reader_grant, approval["reader_grant_sha256"])
    credentials = read_private(args.reader_credentials)
    check_grant(grant, credentials, packet, approval)
    ends = min(timestamp(date) for date in (scope["cutoff"], approval["expires_at"], grant["expires_at"]))
    kube = source_client(approval, packet["cluster_id"])
    observation = read_private(args.consumer_check, approval["consumer_check_sha256"])
    observed_at = timestamp(observation["observed_at"])
    if (observation["schema"] != "public-flamingo-consumer-check-v1"
            or observation["cluster_id"] != packet["cluster_id"] or observation["run"] != packet["run"]
            or observation["storage_operator"] != approval["storage_operator"]
            or observation["inventory_secret"] != packet["inventory_secret"]
            or observation["closed_guard"] != approval["closed_guard"]
            or observation["no_live_consumers"] is not True or observation["secret_absent"] is not True
            or observed_at > datetime.now(timezone.utc) or observed_at >= ends):
        raise ValueError("Expected the storage owner's clean pre-issuance inventory in this closed generation")

    def check_consumer_fence():
        def get(kind, name):
            return json.loads(subprocess.check_output([*kube, "--request-timeout=10s", "get", kind, name, "-o", "json"],
                                                       stderr=subprocess.DEVNULL, timeout=15))
        verify_guard(get, packet["run"], "closed", approval["closed_guard"])

    check_consumer_fence()
    objects = json.loads(packet["configmap"]["data"]["objects.json"])
    inventory = presign(objects, credentials, approval["ttl_seconds"], ends)
    probe_versions(inventory)
    for row in inventory:
        query = {k: v[0] for k, v in parse_qs(urlsplit(row["url"]).query).items()}
        validate_query(query, row["version_id"])
    check_consumer_fence()
    print(json.dumps(create_secret(kube, packet["inventory_secret"], inventory), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except RuntimeError:
        sys.exit("Secret creation was not confirmed. Inspect the exact approved name and UID before any retry.")
    except Exception:
        # HTTP/API/SDK exceptions may contain a signed request or the entire Secret.
        sys.exit("Public inventory issuance failed. Check the independent approvals, target, role and term.")
