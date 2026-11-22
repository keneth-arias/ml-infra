"""Populate two preowned public-baseline leaves from a pinned, versioned inventory."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

HOST = "storage.eu-south1.nebius.cloud"
BUCKET = "airaware-artifacts-5da804df513170d0"
WEIGHTS = "7f8562992f1cf3852ced581f331247c6420fc90292d45453424c77f0a29c199f"
UID = 1000
BLOCK = 1024 * 1024
FILES = frozenset({
    "SHA256SUMS", "weights/added_tokens.json", "weights/config.json",
    "weights/generation_config.json", "weights/model.safetensors",
    "weights/special_tokens_map.json", "weights/tokenizer.json",
    "weights/tokenizer.model", "weights/tokenizer_config.json",
})
PARTIAL = ".public-flamingo-stage"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate inventory field")
        result[key] = value
    return result


def read_config(path):
    # Kubernetes projects ConfigMap and Secret files through read-only symlinks.
    with path.open("rb") as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("Oversized staging metadata")
    return data


def by_name(rows, fields):
    if not isinstance(rows, list) or len(rows) != len(FILES):
        raise ValueError("Expected the nine public objects")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != fields or row["name"] not in FILES:
            raise ValueError("Unexpected object or fields in staging inventory")
        if (type(row["size"]) is not int or not 0 < row["size"] < 14_000_000_000
                or not isinstance(row["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", row["sha256"])
                or not isinstance(row["version_id"], str) or not row["version_id"] or row["version_id"] == "null"):
            raise ValueError("Invalid size, version or checksum")
        result[row["name"]] = row
    if result.keys() != FILES or sum(row["size"] for row in rows) > 14_000_000_000:
        raise ValueError("Unexpected public object set")
    return result


def validate_query(query, version):
    required = {"versionId", "X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires",
                "X-Amz-SignedHeaders", "X-Amz-Signature"}
    if (set(query) not in (required, required | {"X-Amz-Security-Token"}) or query["versionId"] != version
            or query["X-Amz-Algorithm"] != "AWS4-HMAC-SHA256" or query["X-Amz-SignedHeaders"] != "host"
            or not re.fullmatch("[a-f0-9]{64}", query["X-Amz-Signature"])
            or not re.fullmatch("[0-9]{8}T[0-9]{6}Z", query["X-Amz-Date"])
            or not re.fullmatch("[1-9][0-9]{0,3}", query["X-Amz-Expires"])
            or not query.get("X-Amz-Security-Token", "absent")):
        raise ValueError("Invalid version-specific presigned GET")
    credential_scope = query["X-Amz-Credential"].split("/")
    if (len(credential_scope) != 5 or not credential_scope[0]
            or credential_scope[1:] != [query["X-Amz-Date"][:8], "eu-south1", "s3", "aws4_request"]):
        raise ValueError("Invalid presigned GET credential scope")
    try:
        issued = datetime.strptime(query["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError("Invalid presigned GET date") from None
    lifetime = int(query["X-Amz-Expires"])
    now = datetime.now(timezone.utc)
    if lifetime > 1800 or now < issued or now >= issued + timedelta(seconds=lifetime):
        raise ValueError("Presigned GET outside the bounded staging window")


def validated_inventory(pin, program, source):
    data = read_config(program)
    if hashlib.sha256(data).hexdigest() != pin:
        raise ValueError("Public object manifest differs from the approved digest")
    fields = {"name", "size", "version_id", "sha256"}
    expected = by_name(json.loads(data, object_pairs_hook=unique_pairs), fields | {"bucket", "key"})
    inventory = json.loads(read_config(source), object_pairs_hook=unique_pairs)
    if not isinstance(inventory, dict) or set(inventory) != {"base"}:
        raise ValueError("Expected public base objects only")
    records = by_name(inventory["base"], fields | {"url"})
    if expected["SHA256SUMS"]["sha256"] != WEIGHTS or expected["SHA256SUMS"]["size"] > 4096:
        raise ValueError("Unexpected public weights manifest")

    for name, row in records.items():
        bound = expected[name]
        key = f"model-qualification/{WEIGHTS}/base/{name}"
        if (any(row[f] != bound[f] for f in fields) or bound["bucket"] != BUCKET or bound["key"] != key):
            raise ValueError("Source differs from approved public object versions")
        url = row["url"]
        if (not isinstance(url, str) or not url.isascii() or not url.startswith(f"https://{HOST}/{BUCKET}/{key}?")
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            raise ValueError("Invalid versioned object URL")
        parsed = urlsplit(url)
        # Do not include the presigned URL or its query in exceptions.
        try:
            query = unique_pairs(parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16))
        except ValueError:
            raise ValueError("Invalid versioned object query") from None
        validate_query(query, row["version_id"])
        if (parsed.scheme != "https" or parsed.netloc != HOST or parsed.path != f"/{BUCKET}/{key}"
                or parsed.fragment):
            raise ValueError("Unexpected object endpoint, key or version")
    return records


@contextmanager
def locked_leaf(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(fd).st_uid != UID:
            raise ValueError("Staging leaf is not owned by the expected user")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def children(directory, allowed):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != UID:
        raise ValueError("Unexpected staging directory type or owner")
    names = {entry.name for entry in directory.iterdir()}
    if not names <= allowed:
        raise ValueError("Unexpected retained entries on the staging leaf")
    return names


def check_file(info):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != UID:
        raise ValueError("Unexpected staging file type, links or owner")


def stream_digest(stream, size, target=None):
    digest = hashlib.sha256()
    while size:
        data = stream.read(min(BLOCK, size))
        if not data or len(data) > size:
            raise ValueError("Unexpected public object length")
        digest.update(data)
        if target is not None:
            target.write(data)
        size -= len(data)
    if stream.read(1):
        raise ValueError("Oversized public object")
    return digest.hexdigest()


def verify_file(path, row):
    check_file(path.lstat())
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        check_file(info)
        if info.st_size != row["size"] or stream_digest(stream, info.st_size) != row["sha256"]:
            raise ValueError("Staged object size or digest mismatch")


def download(row, scratch, target, fetch):
    # Network exceptions may contain signed query strings. Report a public name only.
    try:
        response = fetch(row["url"])
        with response, scratch.open("xb") as dest:
            if (response.status != 200 or response.headers.get("x-amz-version-id") != row["version_id"]
                    or response.headers.get("Content-Length") != str(row["size"])
                    or response.headers.get("Content-Encoding", "identity") != "identity"):
                raise ValueError("Invalid response metadata")
            if stream_digest(response, row["size"], dest) != row["sha256"]:
                raise ValueError("Incorrect downloaded object")
            dest.flush()
            os.fsync(dest)
    except Exception:
        raise ValueError("Unable to verify downloaded public object " + row["name"]) from None
    os.rename(scratch, target)
    sync_dir(target.parent)


def verify_snapshot(weights_dir, sums, records):
    expected_names = {Path(name).name for name in FILES if name != "SHA256SUMS"}
    if children(weights_dir, expected_names) != expected_names:
        raise ValueError("Incomplete weights directory")
    verify_file(sums, records["SHA256SUMS"])
    lines = [f"{records[name]['sha256']}  {name}" for name in sorted(FILES) if name != "SHA256SUMS"]
    if sums.read_bytes() != ("\n".join(lines) + "\n").encode("ascii"):
        raise ValueError("SHA256SUMS differs from approved weight file list")
    for name in expected_names:
        verify_file(weights_dir / name, records["weights/" + name])


def publish(base, records, fetch):
    pending = base / PARTIAL
    sums = base / "SHA256SUMS"
    weights = base / "weights"
    entries = children(base, {PARTIAL, "SHA256SUMS", "weights"})
    if "SHA256SUMS" in entries:
        verify_snapshot(weights, sums, records)
        if PARTIAL in entries:
            children(pending, set())
            pending.rmdir()
            sync_dir(base)
        return

    if PARTIAL not in entries:
        if entries:
            raise ValueError("Unrecognized incomplete snapshot")
        pending.mkdir(mode=0o700)
        sync_dir(base)
    contents = children(pending, {"weights", "SHA256SUMS", "download.part"})
    scratch = pending / "download.part"
    if "weights" in entries:
        # A crash between the two renames leaves the already verified metadata in pending.
        if contents != {"SHA256SUMS"}:
            raise ValueError("Unexpected snapshot publication state")
        verify_snapshot(weights, pending / "SHA256SUMS", records)
    else:
        partial_weights = pending / "weights"
        if "weights" not in contents:
            partial_weights.mkdir(mode=0o700)
            sync_dir(pending)
        children(partial_weights, {Path(name).name for name in FILES if name.startswith("weights/")})
        if "download.part" in contents:
            check_file(scratch.lstat())
            scratch.unlink()
        missing = []
        for name, row in sorted(records.items()):
            target = pending / name
            if os.path.lexists(target):
                verify_file(target, row)
            else:
                missing.append((target, row))
        if sum(row["size"] for _, row in missing) > shutil.disk_usage(pending).free:
            raise ValueError("Insufficient space for the public snapshot")
        for target, row in missing:
            download(row, scratch, target, fetch)
        verify_snapshot(partial_weights, pending / "SHA256SUMS", records)
        os.rename(partial_weights, weights)
        sync_dir(pending)
        sync_dir(base)
    os.rename(pending / "SHA256SUMS", sums)
    sync_dir(pending)
    sync_dir(base)
    pending.rmdir()
    sync_dir(base)


def stage(pin, program, source, base, output, fetch):
    if os.geteuid() != UID:
        raise ValueError("Unexpected staging identity")
    records = validated_inventory(pin, program, source)
    if base.samefile(output):
        raise ValueError("Separate weights and run output leaves are required")
    with locked_leaf(base), locked_leaf(output):
        children(output, set())
        # The retained ext4 volume supports an unnamed write probe, including after a crash.
        probe = os.open(output, os.O_TMPFILE | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.write(probe, b"1")
            os.fsync(probe)
        finally:
            os.close(probe)
        publish(base, records, fetch)
        children(output, set())
    return {"weights_manifest_sha256": WEIGHTS, "output_uid": UID, "output_empty": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("objects_sha256")
    args = parser.parse_args()
    os.umask(0o077)
    client = build_opener(ProxyHandler({}), NoRedirect())
    # Treat all exceptions from the private inventory and HTTP handling as confidential.
    try:
        receipt = stage(args.objects_sha256, Path("/program/objects.json"), Path("/source/inventory.json"),
                    Path("/base"), Path("/output"), lambda url: client.open(url, timeout=30))
    except Exception:
        raise SystemExit("Public staging failed. Verify the approved inputs, leaves and exclusive handoff.") from None
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
