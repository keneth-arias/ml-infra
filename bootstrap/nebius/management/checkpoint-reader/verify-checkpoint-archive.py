#!/usr/bin/env python3
"""Verify an attended synthetic tar without extracting or deserializing payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tarfile


def regular_private(path):
    entry = path.lstat()
    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
        raise ValueError("Expected an owner-only regular file")
    return entry.st_size


def verify(path, receipt, max_bytes, run, ceiling):
    size = regular_private(path)
    if not 0 < size <= receipt["archive_upper_bound"] <= max_bytes <= 8 * 2**30:
        raise ValueError("Archive exceeds its approved bound")
    expected = {}
    for directory, generation in receipt["generations"].items():
        if not re.fullmatch(r"step-[0-9]{8}", directory):
            raise ValueError("Wrong archive generation directory")
        expected.update({f"{directory}/{name}": entry for name, entry in generation["files"].items()})
        expected[f"{directory}/manifest.json"] = {"sha256": generation["manifest_sha256"]}
    if receipt["terminal"] is not None:
        expected["direct-result.json"] = {"sha256": receipt["terminal"]["receipt_sha256"],
                                          "size": receipt["terminal"]["receipt_bytes"]}
    step, digest = receipt["checkpoint"].split(":")
    if (receipt["logical_run"] != run or not 0 < int(step) <= ceiling
            or expected[f"step-{int(step):08d}/manifest.json"]["sha256"] != digest):
        raise ValueError("Archive generation differs from the intended control")
    with tarfile.open(path, mode="r:") as archive:
        for member in archive:
            approved = expected.pop(member.name, None)
            if (not member.isreg() or approved is None or member.size != approved.get("size", member.size)
                    or member.size > max_bytes or
                    not re.fullmatch(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*", member.name)
                    or any(component in (".", "..") for component in member.name.split("/"))):
                raise ValueError("Unexpected, duplicate or unsafe checkpoint member")
            with archive.extractfile(member) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != approved["sha256"]:
                    raise ValueError("Archive member digest differs from the CSI receipt")
    if expected:
        raise ValueError("Missing archived checkpoint members")
    with path.open("rb") as stream:
        return {"checkpoint": receipt["checkpoint"], "archive_bytes": size,
                "archive_sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--ceiling", type=int, required=True)
    args = parser.parse_args()
    regular_private(args.receipt)
    print(json.dumps(verify(args.archive, json.loads(args.receipt.read_bytes()), args.max_bytes, args.run, args.ceiling), sort_keys=True))
