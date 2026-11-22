#!/usr/bin/env python3
"""Inspect one fenced checkpoint or stream an approved synthetic archive."""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
from time import sleep

ROOT = Path("/state")
MAX_GENERATIONS = 8
MAX_FILES = 512
MAX_METADATA = 2 * 1024 * 1024
IDENTITY_FIELDS = {
    "logical_run", "seed", "head", "direct_objective", "schedule",
    "input_manifest_sha256", "validation_manifest_sha256", "weights",
}
FLAMINGO_FIELDS = {"backbone", "binding_sha256"}
EVAL_INTERVAL = 500
PATIENCE = 4
MIN_IMPROVEMENT = Decimal("0.02")


def checkpoint_api():
    # The immutable signed preflight image supplies this module.
    sys.path.insert(0, "/opt/forecast/src")
    from opentslm import forecast_checkpoint
    return forecast_checkpoint


def verify_identity(identity):
    fields = set(identity)
    flamingo = fields == IDENTITY_FIELDS | FLAMINGO_FIELDS
    if ((fields != IDENTITY_FIELDS and not flamingo)
            or (flamingo and (identity["backbone"] != "flamingo"
                             or not re.fullmatch("[a-f0-9]{64}", identity["binding_sha256"])))
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", identity["logical_run"])
            or identity["head"] != "direct" or identity["direct_objective"] not in ("normalized-mse", "physical-mae")
            or type(identity["seed"]) is not int or not 0 <= identity["seed"] < 2**32
            or type(identity["schedule"]) is not int or not 0 < identity["schedule"] < 10**8
            or any(not re.fullmatch("[a-f0-9]{64}", identity[field]) for field in (
                "input_manifest_sha256", "validation_manifest_sha256", "weights",
            ))):
        raise ValueError("Expected the exact approved direct checkpoint identity")


def load_metadata(path):
    mode = path.lstat()
    if not stat.S_ISREG(mode.st_mode) or not 0 < mode.st_size <= MAX_METADATA:
        raise ValueError("Expected bounded regular checkpoint metadata")
    return json.loads(path.read_bytes())


def safe_files(generation, bound):
    if generation.is_symlink() or not generation.is_dir():
        raise ValueError("Generation must be a real directory")
    count, size = 0, 0
    pending = [generation]
    while pending:
        for path in pending.pop().iterdir():
            mode = path.lstat()
            if stat.S_ISDIR(mode.st_mode):
                pending.append(path)
            elif stat.S_ISREG(mode.st_mode):
                size += mode.st_size
            else:
                raise ValueError("Unexpected link or special file in the checkpoint")
            count += 1
            if count > MAX_FILES or size > bound:
                raise ValueError("Checkpoint exceeds its funded read bound")
    for path in (generation/"manifest.json", generation/"training.pt.json"):
        if path.exists():
            load_metadata(path)
    return size


def verified_flamingo_stop(expected, result, final, history, selected_history):
    identity = result["identity"]
    # The producer hashes its recipe before adding run-specific and model-load metadata.
    excluded = {"logical_run", "recipe_sha256", "frozen_parameters_sha256",
                "trainable_parameters", "trainable_bytes", "initial_trainable_sha256"}
    recipe = {k: v for k, v in identity.items() if k not in excluded}
    recipe_pin = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
    if (identity.get("recipe_sha256") != recipe_pin or
            (recipe.get("evaluation_interval"), recipe.get("patience_checks"),
             recipe.get("min_improvement_ug_m3")) != (EVAL_INTERVAL, PATIENCE, str(MIN_IMPROVEMENT))):
        raise ValueError("Flamingo training recipe differs from the terminal checkpoint")

    ceiling, stop = expected["schedule"], result["step"]
    steps = [*range(0, stop + 1, EVAL_INTERVAL)]
    if stop == ceiling and steps[-1] != stop:
        steps.append(stop)
    if steps[-1] != stop or [event["step"] for event in history] != steps:
        raise ValueError("Flamingo training history differs from the scheduled evaluations")
    best_step = best_mae = None
    patience = 0
    for step, event in zip(steps, history, strict=True):
        score = event["inner_validation"]["mae"]
        if type(score) not in (int, float) or not Decimal(str(score)).is_finite() or score < 0:
            raise ValueError("Invalid physical MAE in Flamingo training history")
        if step >= EVAL_INTERVAL:
            if best_mae is None or Decimal(str(best_mae)) - Decimal(str(score)) >= MIN_IMPROVEMENT:
                best_step, best_mae, patience = step, score, 0
            else:
                patience += 1
        outcome = "completed" if step == ceiling else "early-stopped" if patience >= PATIENCE else "running"
        next_step = min(ceiling, step + EVAL_INTERVAL) if outcome == "running" else None
        selection = (event.get("best_step"), event.get("best_mae"), event.get("patience"),
                     event.get("outcome"), event.get("next_eval_step"))
        if (selection != (best_step, best_mae, patience, outcome, next_step)
                or (outcome != "running") != (step == stop)):
            raise ValueError("Flamingo evaluation history has an inconsistent promotion or training stop")
    if selected_history != history[:steps.index(best_step) + 1]:
        raise ValueError("Selected Flamingo checkpoint does not carry the promoted evaluation history")
    return {k: v for k, v in final.items() if k != "receipt_bytes"} | {
        "identity": expected | {"recipe_sha256": recipe_pin},
    }


def inspect(root, max_bytes, expected, checkpoint):
    verify_identity(expected)
    run, ceiling = expected["logical_run"], expected["schedule"]
    if not 0 < max_bytes <= 8 * 2**30:
        raise ValueError("Expected approved positive checkpoint bound limited to 8 GiB")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected the exact read-only run state directory")
    candidates = sorted((item for item in root.iterdir() if re.fullmatch(r"step-[0-9]{8}", item.name)), reverse=True)
    if len(candidates) > MAX_GENERATIONS:
        raise ValueError("Too many generations for the approved qualification")
    sizes = {path: safe_files(path, max_bytes) for path in candidates}
    if sum(sizes.values()) > max_bytes:
        raise ValueError("Candidate generations exceed the approved cumulative read bound")
    verified = None
    for path in candidates:
        manifest = checkpoint.read_manifest(path)
        if manifest is None:
            continue
        identity = manifest["identity"]
        if any(identity.get(key) != value for key, value in expected.items()):
            raise ValueError("A generation carries a different run identity")
        dependencies = checkpoint.direct_dependencies(path, identity)
        if not dependencies:
            continue
        verified = path, manifest, dependencies
        break

    result_path = root/"direct-result.json"
    if verified is None:
        if result_path.exists() or result_path.is_symlink():
            raise ValueError("Published terminal result lacks its intact generation")
        return {"logical_run": run, "checkpoint": None, "directories_observed": len(candidates)}, {}
    latest_dir, latest, dependencies = verified
    if not 0 < latest["step"] <= ceiling:
        raise ValueError("Checkpoint step exceeds frozen qualification")
    kept = {}
    for path in sorted(dependencies):
        record = checkpoint.read_manifest(path)
        if record["files"] != checkpoint.files(path):
            raise ValueError("Checkpoint payload changed")
        kept[path.name] = {"manifest_sha256": checkpoint.sha256(path/"manifest.json"), "files": record["files"]}
    final = None
    training_stop = {}
    if result_path.exists() or result_path.is_symlink():
        result = load_metadata(result_path)
        selected = result["best_step"]
        if type(selected) is not int or not 0 < selected <= latest["step"]:
            raise ValueError("Invalid terminal selected step")
        selected_name = f"step-{selected:08d}"
        history = load_metadata(latest_dir/"training.pt.json")["evaluations"]
        pointer = root/"best"
        if (pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 64
                or pointer.read_text().strip() != selected_name or selected_name not in kept
                or result["identity"] != latest["identity"] or result["step"] != latest["step"]
                or result["outcome"] != ("completed" if latest["step"] == ceiling else "early-stopped")
                or result["latest_manifest_sha256"] != kept[latest_dir.name]["manifest_sha256"]
                or result["selected_manifest_sha256"] != kept[selected_name]["manifest_sha256"]
                or not history or any(result.get(k) != v for k,v in history[-1].items())):
            raise ValueError("Terminal result and checkpoint manifest differ")
        final = {key: result[key] for key in (
            "step", "best_step", "outcome", "latest_manifest_sha256", "selected_manifest_sha256",
        )}
        final.update(receipt_sha256=checkpoint.sha256(result_path), receipt_bytes=result_path.stat().st_size)
        if expected.get("backbone") == "flamingo":
            selected_history = load_metadata(root/selected_name/"training.pt.json")["evaluations"]
            stop = verified_flamingo_stop(expected, result, final, history, selected_history)
            training_stop = {
                "flamingo_training_stop": stop,
                "flamingo_training_stop_sha256": hashlib.sha256(json.dumps(stop, sort_keys=True).encode()).hexdigest(),
            }
    archive_files = {
        f"{path.name}/{name}": path/name for path in sorted(dependencies)
        for name in [*kept[path.name]["files"], "manifest.json"]
    }
    if final is not None:
        archive_files[result_path.name] = result_path
    payload_bytes = sum(sizes[p] for p in dependencies) + (result_path.stat().st_size if final else 0)
    archive_bound = payload_bytes + len(archive_files)*2048 + 10240
    if archive_bound > max_bytes:
        raise ValueError("Combined checkpoint archive exceeds funded read bound")
    receipt = {"logical_run": run, "checkpoint": f"{latest['step']}:{kept[latest_dir.name]['manifest_sha256']}",
               "latest_generation": latest_dir.name, "generations": kept, "terminal": final,
               "payload_bytes": payload_bytes, "archive_upper_bound": archive_bound, **training_stop}
    return receipt, archive_files


def emit_archive(stream, members):
    with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as archive:
        for name, path in sorted(members.items()):
            if len(name) > 200 or not re.fullmatch(r"[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*", name):
                raise ValueError("Unexpected checkpoint archive name")
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            with path.open("rb") as body:
                info.size = path.stat().st_size
                archive.addfile(info, body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-bytes", type=int)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--wait-for-operator", action="store_true")
    parser.add_argument("--export-checkpoint", help="Exact STEP:SHA256 returned by a separate inspect")
    args = parser.parse_args()
    if not ROOT.is_mount() or not os.statvfs(ROOT).f_flag & os.ST_RDONLY:
        raise ValueError("Checkpoint mount is not read-only")
    if args.wait_for_operator:
        if args.max_bytes is not None or args.export_checkpoint is not None or args.identity is not None:
            parser.error("Wait mode does not read any checkpoint")
        sleep(600)
        return
    if args.max_bytes is None or args.identity is None:
        parser.error("Supply the independently approved identity and funded byte limit")
    expected = load_metadata(args.identity)
    receipt, members = inspect(ROOT, args.max_bytes, expected, checkpoint_api())
    if args.export_checkpoint is None:
        print(json.dumps(receipt, sort_keys=True))
    else:
        if not receipt["checkpoint"] or args.export_checkpoint != receipt["checkpoint"]:
            raise ValueError("Checkpoint differs from the approved export")
        emit_archive(sys.stdout.buffer, members)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, TypeError) as error:
        raise SystemExit(f"Fenced checkpoint reader stopped: {error}") from None
