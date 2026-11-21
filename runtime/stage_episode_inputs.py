"""Publish a verified base and phase bundle on a private POSIX input volume."""

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from episode_job import bundle, bundle_manifest, verify_base
from versioned_episode_inputs import ObjectSet, sources


def digest(value):
    if not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("Expected a lowercase SHA-256 digest")
    return value


def tree_size(directory):
    size = 0
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"Expected a source directory: {directory}")
    for path in directory.rglob("*"):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            size += info.st_size
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Unsupported staging input: {path.name}")
    return size


def sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def discard_partial(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except PermissionError:
        # Local copies can inherit read-only directory modes from the source.
        # Only change staging directories, never files or symlink targets.
        if path.is_symlink():
            raise
        path.chmod(0o700)
        for directory, children, _ in os.walk(path):
            for child in children:
                folder = Path(directory) / child
                if not folder.is_symlink():
                    folder.chmod(0o700)
        shutil.rmtree(path)


def publish(parent, key, source, verify, *, check_manifest=None):
    started = time.monotonic()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    final = parent / digest(key)
    with (parent / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Only a previous holder of this digest's lock could have left this copy.
        partial = parent / (".staging-" + key)
        discard_partial(partial)
        if final.exists():
            tree_size(final)
            verify(final)
            outcome = "hit"
        else:
            if (
                isinstance(source, Path)
                and tree_size(source) > shutil.disk_usage(parent).free
            ):
                raise OSError("Insufficient free space for staged revision")
            partial.mkdir(mode=0o700)
            try:
                stage = partial / "contents"
                if isinstance(source, ObjectSet):
                    source.copy(stage, check_manifest)
                else:
                    if check_manifest:
                        check_manifest(source / "manifest.json")
                    shutil.copytree(source, stage, symlinks=True)
                    # Cross-parent rename needs write permission on the copied directory.
                    stage.chmod(stat.S_IMODE(stage.stat().st_mode) | stat.S_IWUSR)
                tree_size(stage)
                verify(stage)
                for directory, _, names in os.walk(stage, topdown=False):
                    for name in names:
                        sync(Path(directory) / name)
                    sync(directory)
                stage.rename(final)
                sync(parent)
            finally:
                discard_partial(partial)
            outcome = "miss"
    return {"revision": key, "cache": outcome, "seconds": time.monotonic() - started}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", type=Path)
    p.add_argument("--bundle", type=Path)
    p.add_argument("--validation-bundle", type=Path)
    p.add_argument("--validation-sha256", type=digest)
    p.add_argument(
        "--inventory", type=Path, help="Mounted Secret with versioned object URLs"
    )
    p.add_argument(
        "--source-prefix", help="Independently approved source bucket and key prefix"
    )
    p.add_argument(
        "--cache", type=Path, required=True, help="Private airaware/cache directory"
    )
    p.add_argument("--weights-sha256", required=True, type=digest)
    p.add_argument("--bundle-sha256", required=True, type=digest)
    p.add_argument("--phase", required=True, choices=("fit", "infer"))
    p.add_argument("--head", required=True, choices=("episode", "direct", "quantile"))
    a = p.parse_args()

    paired = a.validation_sha256 is not None
    if paired and (
        (a.phase, a.head) != ("fit", "direct") or a.validation_sha256 == a.bundle_sha256
    ):
        p.error(
            "Paired staging requires distinct fit and inner-validation digests for the direct head"
        )
    specs = {}

    def stage_role(role, key, source):
        def check_spec(spec):
            if spec["weights_manifest_sha256"] != a.weights_sha256:
                raise ValueError("Phase bundle requires a different weights revision")
            if role == "inner_validation":
                fit = specs["fit"]
                if (
                    any(
                        spec[k] != fit[k]
                        for k in ("protocol_sha256", "source_manifest_sha256")
                    )
                    or spec["files"]["thresholds.json"]
                    != fit["files"]["thresholds.json"]
                    or spec["ids_sha256"] == fit["ids_sha256"]
                ):
                    raise ValueError(
                        "Fit and validation do not share the frozen cohort protocol"
                    )

        def verify(path):
            spec = bundle(path, key, role, a.head, paired=paired)
            check_spec(spec)
            if paired:
                threshold = json.loads((path / "thresholds.json").read_bytes())
                fit = spec if role == "fit" else specs["fit"]
                if (
                    threshold.get("fit_ids_sha256") != fit["ids_sha256"]
                    or threshold.get("unit") != "ug/m3"
                ):
                    raise ValueError("Threshold provenance differs from the fit cohort")
            specs[role] = spec

        def precheck(manifest):
            check_spec(bundle_manifest(manifest, key, role, a.head, paired=True))

        return publish(
            a.cache / "bundles",
            key,
            source,
            verify,
            check_manifest=precheck if paired else None,
        )

    def verify_weights(path):
        if {p.name for p in path.iterdir()} != {"weights", "SHA256SUMS"}:
            raise ValueError("Unexpected base files")
        verify_base(path / "weights", a.weights_sha256)

    if a.inventory:
        if a.base or a.bundle or a.validation_bundle or not a.source_prefix:
            p.error(
                "Use inventory and source-prefix together, in place of local inputs"
            )
        inputs = sources(a.inventory, a.source_prefix, paired=paired)
        a.base, a.bundle = inputs[:2]
        if paired:
            a.validation_bundle = inputs[2]
    else:
        if (
            not a.bundle
            or not a.base
            or a.source_prefix
            or bool(a.validation_bundle) != paired
        ):
            p.error("Supply both local directories or a versioned object inventory")
        local_sources = [a.base, a.bundle]
        if paired:
            local_sources.append(a.validation_bundle)
        for source in local_sources:
            if a.cache.resolve().is_relative_to(source.resolve()):
                p.error("Cache must be outside the input trees")
    result = {"phase": stage_role(a.phase, a.bundle_sha256, a.bundle)}
    if paired:
        result["inner_validation"] = stage_role(
            "inner_validation", a.validation_sha256, a.validation_bundle
        )
    result["weights"] = publish(
        a.cache / "weights", a.weights_sha256, a.base, verify_weights
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
