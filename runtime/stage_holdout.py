"""Stage a history-only external test bundle for an already retained concentration release."""

import argparse
import fcntl
import json
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from episode_job import bundle
from episode_release import retain, sha


def stage(cohort, cohort_digest, inputs_root, state_root, run_id, release_digest, step):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}[a-z0-9]", run_id):
        raise ValueError("Invalid run ID")
    if not re.fullmatch(r"[a-f0-9]{64}", release_digest) or step < 1:
        raise ValueError("Invalid release digest or step")
    receipt_path = cohort / "receipt.json"
    if sha(receipt_path) != cohort_digest:
        raise ValueError("Cohort digest mismatch")
    receipt = json.loads(receipt_path.read_bytes())
    retained = state_root / "episodes/retained"
    release = retained / release_digest
    manifest = release / "release.json"
    generation = release / f"step-{step:08d}"
    retain(manifest, release_digest, generation, retained)
    spec = json.loads(manifest.read_bytes())
    seal = json.loads((release / "policy.json").read_bytes())
    seal_sha = spec["components"]["policy.json"]
    head = seal["head"]
    if (
        receipt["version"] != "lima-cohorte-campo-marte-v1"
        or spec["mode"] != "concentration"
        or spec.get("purpose") != "retrospective_holdout_only"
        or seal["version"] != "lima-concentracion-prueba-congelada-v1"
        or seal["scope"] != "retrospective_concentration_only"
        or head not in ("direct", "quantile")
        or receipt["seal_sha256"] != seal_sha
        or receipt["protocol_sha256"] != seal["protocol_sha256"]
        or spec["image"] != seal["image"]
        or spec["base_manifest_sha256"] != seal["base_weights_sha256"]
        or spec["checkpoint_sha256"] != seal["checkpoint_sha256"]
    ):
        raise ValueError("Release is incompatible with the sealed external cohort")
    digest = receipt["files"]["infer/manifest.json"]
    source = cohort / "infer"
    input_spec = bundle(source, digest, "infer", head)
    if (
        input_spec["schema"] != "forecast-input-v1"
        or input_spec["unit"] != "ug/m3"
        or input_spec["block"] != "external_test"
        or input_spec["seal_sha256"] != seal_sha
        or input_spec["checkpoint_sha256"] != seal["checkpoint_sha256"]
        or input_spec["weights_manifest_sha256"] != seal["base_weights_sha256"]
    ):
        raise ValueError("Inference bundle belongs to another release")
    if receipt["eligible_histories"] == 0:
        return {"status": "empty", "bundle_sha256": digest}
    # The inference Job mounts this subdirectory and its own output independently.
    target = inputs_root / "airaware/episodes" / run_id / "infer"
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / ".stage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            bundle(target, digest, "infer", head)
        else:
            with TemporaryDirectory(dir=target.parent) as tmp:
                staged = Path(tmp) / "infer"
                shutil.copytree(source, staged, symlinks=True)
                bundle(staged, digest, "infer", head)
                staged.rename(target)
        (state_root / "episodes" / run_id / "output").mkdir(parents=True, exist_ok=True)
    return {
        "status": "ready",
        "run_id": run_id,
        "release_json": manifest.read_text(),
        "seal_json": (release / "policy.json").read_text(),
        "checkpoint_manifest_json": (generation / "manifest.json").read_text(),
        "input_manifest_json": (source / "manifest.json").read_text(),
        "bundle_sha256": digest,
        "head": head,
        "image": seal["image"],
        "seed": seal["seed"],
        "step": step,
        "retained_release": release_digest,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("cohort", "inputs_root", "state_root"):
        p.add_argument(name, type=Path)
    p.add_argument("run_id")
    p.add_argument("--cohort-sha256", required=True)
    p.add_argument("--retained-release", required=True)
    p.add_argument("--step", required=True, type=int)
    a = p.parse_args()
    result = stage(
        a.cohort,
        a.cohort_sha256,
        a.inputs_root,
        a.state_root,
        a.run_id,
        a.retained_release,
        a.step,
    )
    print(json.dumps(result, sort_keys=True))
