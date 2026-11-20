"""Stage deterministic synthetic fit inputs or AirAware fixture histories for one run."""

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(phase, head, fixture, output, weights, checkpoint):
    source = json.loads(fixture.read_text())
    if source["version"] != "lima-episode-fixture-v1" or source["unit"] != "ug/m3":
        raise ValueError("Unexpected fixture version or units")
    table = {}
    for station, limit in zip(
        source["station_key"], source["threshold_ug_m3"], strict=True
    ):
        if table.setdefault(station, limit) != limit:
            raise ValueError("Conflicting station thresholds")
    if output.exists():
        raise FileExistsError(output)
    if (phase == "infer") != (checkpoint is not None):
        raise ValueError("Inference requires the selected checkpoint manifest")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as tmp:
        folder = Path(tmp) / "bundle"
        folder.mkdir()
        if phase == "fit":
            rng = np.random.default_rng(27)
            history = rng.uniform(5, 35, size=(33, 24)).astype("f4")
            targets = history[:, -1:] + rng.normal(0, 3, size=(33, 6))
            ids = [f"train-{n}" for n in range(len(history))]
            stations = np.resize(sorted(table), len(ids))
        else:
            history = np.asarray(source["history"], dtype="f4")
            ids = source["window_id"]
            stations = source["station_key"]
        spec = {
            "schema": "forecast-input-v1",
            "unit": "ug/m3",
            "head": head,
            "phase": phase,
            "weights_manifest_sha256": digest(weights),
            "fixture_sha256": digest(fixture),
            "files": {},
        }
        rows = {"window_id": ids, "history": history}
        if head == "episode":
            limits = [table[s] for s in stations]
            path = folder / "thresholds.json"
            path.write_text(
                json.dumps({"unit": "ug/m3", "stations": table}, sort_keys=True) + "\n"
            )
            spec["thresholds_sha256"] = spec["files"][path.name] = digest(path)
            rows.update(
                station_key=stations,
                threshold_ug_m3=limits,
                thresholds_sha256=spec["thresholds_sha256"],
            )
        if phase == "fit":
            if head == "episode":
                rows["event"] = (targets.max(axis=1) >= limits).astype("i4")
            else:
                rows["targets"] = targets.astype("f4")
        else:
            spec["checkpoint_sha256"] = digest(checkpoint)
        np.savez(folder / "data.npz", **rows)
        spec["files"]["data.npz"] = digest(folder / "data.npz")
        (folder / "manifest.json").write_text(json.dumps(spec, sort_keys=True) + "\n")
        folder.rename(output)
        return digest(output / "manifest.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fit", "infer"))
    parser.add_argument("head", choices=("quantile", "episode", "direct"))
    parser.add_argument("fixture", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("weights_manifest", type=Path)
    parser.add_argument("--checkpoint-manifest", type=Path)
    args = parser.parse_args()
    print(
        prepare(
            args.phase,
            args.head,
            args.fixture,
            args.output,
            args.weights_manifest,
            args.checkpoint_manifest,
        )
    )
