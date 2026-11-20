"""Retain and verify a selected complete model generation and its frozen pilot artifacts."""

import argparse
import fcntl
import hashlib
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory


def sha(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular release artifact at {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(checkpoint, release, components):
    if sha(checkpoint / "manifest.json") != release["checkpoint_sha256"]:
        raise ValueError("Checkpoint manifest checksum mismatch")
    manifest = json.loads((checkpoint / "manifest.json").read_text())
    if checkpoint.name != f"step-{manifest['step']:08d}":
        raise ValueError("Invalid generation")
    files = {
        str(p.relative_to(checkpoint)): p
        for p in checkpoint.rglob("*")
        if p.is_file() and p != checkpoint / "manifest.json"
    }
    if (
        not files
        or set(files) != set(manifest["files"])
        or any(p.is_symlink() for p in checkpoint.rglob("*"))
    ):
        raise ValueError("Incomplete checkpoint")
    for name, p in files.items():
        if {"size": p.stat().st_size, "sha256": sha(p)} != manifest["files"][name]:
            raise ValueError("Damaged checkpoint payload")
    for name, digest in release["components"].items():
        if sha(components / name) != digest:
            raise ValueError(f"Damaged release component {name}")


def retain(manifest, digest, checkpoint, destination):
    if sha(manifest) != digest:
        raise ValueError("Release manifest checksum mismatch")
    spec = json.loads(manifest.read_text())
    if spec.get("version") != 1 or spec.get("mode") not in ("concentration", "episode"):
        raise ValueError("Unsupported release")
    required = {"policy.json", "transform.json", "calibration.json"}
    if spec["mode"] == "episode":
        required.add("thresholds.json")
    if (
        set(spec.get("components", {})) != required
        or not re.fullmatch(r".+@sha256:[a-f0-9]{64}", spec["image"])
        or not re.fullmatch(r"[a-f0-9]{64}", spec["base_manifest_sha256"])
    ):
        raise ValueError("Incomplete release identity")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / digest
    with (destination / ".release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            if sha(target / "release.json") != digest:
                raise ValueError("Damaged retained release")
            verify(target / checkpoint.name, spec, target)
            return target
        verify(checkpoint, spec, manifest.parent)
        with TemporaryDirectory(dir=destination) as tmp:
            stage = Path(tmp) / "release"
            stage.mkdir()
            shutil.copytree(checkpoint, stage / checkpoint.name, symlinks=True)
            for name in required:
                shutil.copyfile(manifest.parent / name, stage / name)
            shutil.copyfile(manifest, stage / "release.json")
            if sha(stage / "release.json") != digest:
                raise ValueError("Release changed during copy")
            verify(stage / checkpoint.name, spec, stage)
            stage.rename(target)
    return target


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest", type=Path)
    p.add_argument("digest")
    p.add_argument("checkpoint", type=Path)
    p.add_argument("destination", type=Path)
    a = p.parse_args()
    print(retain(a.manifest, a.digest, a.checkpoint, a.destination))
