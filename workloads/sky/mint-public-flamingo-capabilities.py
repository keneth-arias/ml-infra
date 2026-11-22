#!/usr/bin/env python3
"""Mint nine expiring public-checkpoint GET capabilities on the custody host."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request

from public_flamingo_stage_worker import unique_pairs

HERE = Path(__file__).resolve().parent
ISSUER = runpy.run_path(str(HERE / "issue-public-flamingo-inventory.py"))
MODEL_OWNER = ISSUER["MODEL_OWNER"]
PROJECT = "project-e07z8wzama002x047kpx72"


def expiration(url):
    query = unique_pairs(parse_qsl(urlsplit(url).query, strict_parsing=True))
    start = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    return start + timedelta(seconds=int(query["X-Amz-Expires"]))


def require_denial(client, method, url):
    try:
        with client.open(Request(url, method=method, headers={"Range": "bytes=0-0"}), timeout=10):
            raise ValueError("Altered capability unexpectedly authorized")
    except HTTPError as error:
        with error:
            if error.code != 403:
                raise ValueError("Altered capability denial unconfirmed") from None


def negative_probes(entries):
    # Change only signed elements. Each probe is non-mutating, and no exception
    # or body from the remote API is emitted to a log.
    client = ISSUER["source_https_client"]()
    parsed = urlsplit(entries[0]["url"])
    signed = unique_pairs(parse_qsl(parsed.query, strict_parsing=True))
    changed = {**signed, "versionId": signed["versionId"] + "-unapproved"}
    list_path = "/" + parsed.path.lstrip("/").split("/", 1)[0]
    require_denial(client, "HEAD", entries[0]["url"])
    require_denial(client, "GET", urlunsplit(parsed._replace(query=urlencode(changed))))
    require_denial(client, "GET", urlunsplit(parsed._replace(path=urlsplit(entries[1]["url"]).path)))
    require_denial(client, "GET", urlunsplit(parsed._replace(path=list_path, query="list-type=2&" + parsed.query)))


def encode(obj):
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_new(path, raw):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def mint(packet, authorization, enrolled, key_id, secret, probe=ISSUER["probe_versions"], deny=negative_probes):
    if (authorization["purpose"] != "mint-public-flamingo-get-capabilities"
            or authorization["project_id"] != packet["project_id"] or packet["project_id"] != PROJECT
            or authorization["model_owner"] != enrolled["model_owner"] or enrolled["model_owner"] != MODEL_OWNER
            or enrolled["project_id"] != PROJECT or enrolled["access_key_id_sha256"] != sha256(key_id.encode()).hexdigest()
            or authorization["objects_sha256"] != packet["objects_sha256"]
            or authorization["run"] != packet["run"] or authorization["inventory_secret"] != packet["inventory_secret"]):
        raise ValueError("Custody approval, enrollment or manifest mismatch")
    if not key_id or not secret:
        raise ValueError("Owner signing key unavailable")
    lifetime = authorization["ttl_seconds"]
    end = ISSUER["timestamp"](authorization["expires_at"])
    objects_raw = packet["configmap"]["data"]["objects.json"].encode()
    if sha256(objects_raw).hexdigest() != packet["objects_sha256"]:
        raise ValueError("Object identities differ from approved manifest")
    objects = json.loads(objects_raw)
    key = {"access_key_id": key_id, "secret_access_key": secret}
    # The SDK applies its own UTC timestamp on each signature. If a second
    # boundary divides this batch, discard it in memory before exposure.
    for _ in range(3):
        rows = ISSUER["presign"](objects, key, lifetime, end)
        times = {expiration(row["url"]) for row in rows}
        if len(times) == 1:
            break
    else:
        raise ValueError("Could not sign one co-expiring set")
    probe(rows)
    deny(rows)
    if datetime.now(timezone.utc) >= next(iter(times)) or next(iter(times)) >= end:
        raise ValueError("Capability expiry is outside the owner window")
    inventory = encode({"base": rows})
    attestation = {
        "schema": "public-flamingo-scoped-get-attestation-v1", "model_owner": MODEL_OWNER,
        "project_id": PROJECT, "objects_sha256": packet["objects_sha256"],
        "inventory_sha256": sha256(inventory).hexdigest(), "expires_at": authorization["expires_at"],
    }
    return inventory, attestation, next(iter(times))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("authorization", "enrollment", "packet", "promotion", "binding", "scope", "output-dir"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--approved-sha256", required=True)
    a = p.parse_args()
    auth = ISSUER["read_private"](a.authorization, a.approved_sha256)
    packet = ISSUER["read_private"](a.packet, auth["packet_file_sha256"])
    enrolled = ISSUER["read_private"](a.enrollment, auth["enrollment_sha256"])
    scope = ISSUER["read_private"](a.scope, ISSUER["SCOPE_SHA"])
    rebuilt = ISSUER["RENDER"](a.promotion, a.binding, auth["deployment_sha256"], auth["run"], auth["inventory_secret"])
    if (rebuilt != packet or auth["project_id"] != scope["project_id"] or packet["cluster_id"] != scope["cluster_id"]
            or ISSUER["timestamp"](auth["expires_at"]) > ISSUER["timestamp"](scope["cutoff"])):
        raise ValueError("Owner scope or packet differs from the active promotion")
    os.umask(0o077)
    # Exclusive directory reserves one issuance. On an ambiguous failure, the
    # operator inspects this directory before any fresh issuance.
    a.output_dir.mkdir(mode=0o700)
    inventory, attestation, end = mint(packet, auth, enrolled, os.environ["AWS_ACCESS_KEY_ID"],
                                       os.environ["AWS_SECRET_ACCESS_KEY"])
    write_new(a.output_dir / "capability-inventory.json", inventory)
    write_new(a.output_dir / "attestation.json", encode(attestation))
    fd = os.open(a.output_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    print(json.dumps({"attestation_sha256": sha256(encode(attestation)).hexdigest(),
                      "inventory_sha256": attestation["inventory_sha256"], "urls_expire_at": end.isoformat()}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit("Public capability mint unconfirmed. Inspect the private issuance directory.")
