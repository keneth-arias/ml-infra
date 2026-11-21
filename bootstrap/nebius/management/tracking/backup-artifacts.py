#!/usr/bin/env python3
"""Back up MLflow artifact bytes while the tracking writer is stopped."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


ENDPOINT = "https://storage.eu-south1.nebius.cloud"
REGION = "eu-south1"
SCHEMA = "airaware-mlflow-artifact-snapshot-v1"
CHUNK_BYTES = 1024 * 1024
MAX_PUT_BYTES = 5 * 1024**3


class BackupError(ValueError):
    """An operator-facing failure that can be reported without object names."""


def require_target(bucket, prefix, max_bytes, max_objects):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise BackupError("Expected a bucket name")
    if not re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)+", prefix):
        raise BackupError("Expected a dedicated artifact prefix ending in a slash")
    if min(max_bytes, max_objects) <= 0:
        raise BackupError("Backup limits must be positive")


def require_versioned(store, bucket):
    if store.get_bucket_versioning(Bucket=bucket).get("Status") != "Enabled":
        raise BackupError("Bucket versioning must be enabled")


def valid_version(version):
    return isinstance(version, str) and version not in ("", "null")


def version_pages(store, bucket, **kwargs):
    return store.get_paginator("list_object_versions").paginate(Bucket=bucket, **kwargs)


def inventory(store, bucket, prefix, max_bytes, max_objects):
    current = {}
    scanned = size = 0
    for page in version_pages(store, bucket, Prefix=prefix):
        versions = page.get("Versions", [])
        scanned += len(versions) + len(page.get("DeleteMarkers", []))
        if scanned > max_objects:
            raise BackupError("Version inventory exceeds the object limit")
        for obj in versions:
            if not obj["IsLatest"]:
                continue
            key = obj["Key"]
            if not key.startswith(prefix) or key in current or not valid_version(obj["VersionId"]):
                raise BackupError("Unexpected current version in the source inventory")
            length = obj["Size"]
            if type(length) is not int or not 0 <= length <= MAX_PUT_BYTES:
                raise BackupError("Artifact exceeds the supported single-object restore size")
            size += length
            if size > max_bytes:
                raise BackupError("Artifact payload exceeds the byte limit")
            current[key] = (obj["VersionId"], length)
    return current


def private_file(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def private_reader(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise BackupError("Expected a private regular snapshot file")
    return os.fdopen(descriptor, "rb")


def require_private_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BackupError("Expected an owner-only snapshot directory")


def read_digest(body, length, output=None):
    sha = hashlib.sha256()
    remaining = length
    while remaining:
        data = body.read(min(remaining, CHUNK_BYTES))
        if not data:
            raise BackupError("Artifact is shorter than its recorded size")
        sha.update(data)
        if output is not None:
            output.write(data)
        remaining -= len(data)
    if body.read(1):
        raise BackupError("Artifact is longer than its recorded size")
    return sha.hexdigest()


def object_digest(store, bucket, key, version, size, output=None):
    obj = store.get_object(Bucket=bucket, Key=key, VersionId=version)
    with closing(obj["Body"]) as body:
        if obj.get("VersionId") != version or obj.get("ContentLength") != size:
            raise BackupError("Artifact version or size differs from the inventory")
        return read_digest(body, size, output)


def snapshot(store, bucket, prefix, directory, max_bytes, max_objects):
    require_target(bucket, prefix, max_bytes, max_objects)
    require_versioned(store, bucket)
    before = inventory(store, bucket, prefix, max_bytes, max_objects)
    directory.mkdir(mode=0o700)
    payloads = directory / "objects"
    payloads.mkdir(mode=0o700)
    artifacts = []
    for index, (key, (version, size)) in enumerate(sorted(before.items())):
        with private_file(payloads / str(index)) as output:
            digest = object_digest(store, bucket, key, version, size, output)
            output.flush()
            os.fsync(output.fileno())
        artifacts.append(dict(key=key, version_id=version, size=size, sha256=digest))
    if inventory(store, bucket, prefix, max_bytes, max_objects) != before:
        raise BackupError("Source inventory changed during backup")
    manifest = dict(
        schema=SCHEMA, endpoint=ENDPOINT, bucket=bucket, prefix=prefix,
        observed_at=datetime.now(timezone.utc).isoformat(), artifacts=artifacts,
    )
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    with private_file(directory / ".manifest.pending") as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    (directory / ".manifest.pending").replace(directory / "manifest.json")
    return dict(objects=len(artifacts), bytes=sum(row["size"] for row in artifacts),
                manifest_sha256=hashlib.sha256(raw).hexdigest())


def load_manifest(directory, bucket, prefix, max_bytes, max_objects):
    require_private_directory(directory)
    require_private_directory(directory / "objects")
    limit = min(max_objects * 8192 + 4096, 64 * 1024**2)
    with private_reader(directory / "manifest.json") as manifest_file:
        raw = manifest_file.read(limit + 1)
    if len(raw) > limit:
        raise BackupError("Manifest exceeds the configured limit")
    manifest = json.loads(raw)
    if manifest["schema"] != SCHEMA or manifest["endpoint"] != ENDPOINT or manifest["prefix"] != prefix:
        raise BackupError("Snapshot schema, endpoint or prefix differs")
    if bucket == manifest["bucket"]:
        raise BackupError("Restore requires a separate isolated bucket")
    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > max_objects:
        raise BackupError("Invalid artifact count in the snapshot")
    size = 0
    keys = set()
    for index, row in enumerate(artifacts):
        key = row["key"]
        length = row["size"]
        if not isinstance(key, str) or not key.startswith(prefix) or key in keys:
            raise BackupError("Invalid or repeated artifact key")
        if (type(length) is not int or not 0 <= length <= MAX_PUT_BYTES
                or not valid_version(row["version_id"])
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])):
            raise BackupError("Invalid artifact metadata")
        keys.add(key)
        size += length
        if size > max_bytes:
            raise BackupError("Artifact payload exceeds the byte limit")
        with private_reader(directory / "objects" / str(index)) as body:
            if read_digest(body, length) != row["sha256"]:
                raise BackupError("Snapshot artifact digest differs")
    return artifacts, size, hashlib.sha256(raw).hexdigest()


def require_empty(store, bucket):
    for page in version_pages(store, bucket):
        if page.get("Versions") or page.get("DeleteMarkers"):
            raise BackupError("Restore bucket contains existing versions or delete markers")
    for page in store.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
        if page.get("Uploads"):
            raise BackupError("Restore bucket contains incomplete uploads")


def restore(store, bucket, prefix, directory, max_bytes, max_objects):
    require_target(bucket, prefix, max_bytes, max_objects)
    artifacts, size, manifest_digest = load_manifest(directory, bucket, prefix, max_bytes, max_objects)
    require_versioned(store, bucket)
    require_empty(store, bucket)
    restored = {}
    for index, row in enumerate(artifacts):
        key, length, digest = row["key"], row["size"], row["sha256"]
        with private_reader(directory / "objects" / str(index)) as body:
            if read_digest(body, length) != digest:
                raise BackupError("Snapshot changed during restore")
            body.seek(0)
            obj = store.put_object(Bucket=bucket, Key=key, Body=body,
                                   ContentLength=length, IfNoneMatch="*")
        version = obj.get("VersionId")
        if not valid_version(version) or object_digest(store, bucket, key, version, length) != digest:
            raise BackupError("Restored artifact version or digest differs")
        restored[key] = (version, length)
    actual = {}
    for page in version_pages(store, bucket):
        if page.get("DeleteMarkers"):
            raise BackupError("Restore bucket acquired an unexpected delete marker")
        for obj in page.get("Versions", []):
            key = obj["Key"]
            value = (obj["VersionId"], obj["Size"])
            if not obj["IsLatest"] or key in actual or restored.get(key) != value:
                raise BackupError("Restore bucket acquired an unexpected version")
            actual[key] = value
    if actual != restored:
        raise BackupError("Restored version inventory differs")
    return dict(objects=len(artifacts), bytes=size, manifest_sha256=manifest_digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("snapshot", "restore"))
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--max-objects", type=int, required=True,
                        help="Maximum artifact records and source versions scanned")
    args = parser.parse_args()
    try:
        import boto3
        from botocore.config import Config

        store = boto3.client("s3", endpoint_url=ENDPOINT, region_name=REGION,
                             config=Config(s3={"addressing_style": "path"}))
        action = snapshot if args.action == "snapshot" else restore
        result = action(store, args.bucket, args.prefix, args.directory, args.max_bytes, args.max_objects)
    except BackupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        # SDK and path errors can embed protected object names and signed request details.
        print(f"Artifact {args.action} failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
