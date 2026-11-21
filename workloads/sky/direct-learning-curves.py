#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["mlflow-skinny==3.18.0", "matplotlib==3.10.7"]
# ///
"""Export one paired learning-curve report from the completed MLflow runs."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from matplotlib.figure import Figure


def read_run(client, run_id, folder):
    run = client.get_run(run_id)
    if not run.data.tags.get("direct_fit_id") or run.info.status != "FINISHED":
        raise ValueError("Expected a finished paired fit")

    def artifact(name):
        return Path(client.download_artifacts(run_id, name, str(folder))).read_bytes()

    raw = artifact("direct-result.json")
    if hashlib.sha256(raw).hexdigest() != run.data.tags["artifact_sha256"]:
        raise ValueError("The terminal result differs from its logged digest")
    result = json.loads(raw)
    manifest = artifact("evaluations/manifest.json")
    if hashlib.sha256(manifest).hexdigest() != result["latest_manifest_sha256"]:
        raise ValueError("Unexpected terminal checkpoint manifest")
    metadata = artifact("evaluations/training.pt.json")
    if hashlib.sha256(metadata).hexdigest() != json.loads(manifest)["files"]["training.pt.json"]["sha256"]:
        raise ValueError("The evaluation history differs from its checkpoint")
    history = json.loads(metadata)["evaluations"]
    if not history or any(result.get(key) != value for key, value in history[-1].items()):
        raise ValueError("The learning curve differs from the terminal evaluation")
    steps = [ev["step"] for ev in history]
    if steps != sorted(set(steps)) or steps[0] != 0 or result["best_step"] not in steps:
        raise ValueError("Invalid evaluation steps")
    return run_id, result, history


def export(records, folder):
    by_seed = {}
    for record in records:
        run_id, result, history = record
        identity = result["identity"]
        pair = by_seed.setdefault(identity["seed"], {})
        arm = identity["direct_objective"]
        if arm in pair:
            raise ValueError("Duplicate objective for this seed")
        pair[arm] = record
    for pair in by_seed.values():
        if set(pair) != {"normalized-mse", "physical-mae"}:
            raise ValueError("Supply both completed arms for each seed")
        identities = [
            {
                k: v
                for k, v in r[1]["identity"].items()
                if k not in ("direct_objective", "logical_run", "recipe_sha256")
            }
            for r in pair.values()
        ]
        if identities[0] != identities[1]:
            raise ValueError("The two runs have different inputs, initialization or training settings")

    figure = Figure(figsize=(12, 3.8 * len(by_seed)), layout="constrained")
    axes = figure.subplots(len(by_seed), 3, squeeze=False)
    with (folder / "curves.csv").open("x", newline="") as table:
        out = csv.writer(table)
        out.writerow(
            [
                "run_id",
                "seed",
                "objective",
                "step",
                "split",
                "manifest_sha256",
                "metric",
                "value",
                "denominator",
                "selected",
                "stopped",
            ]
        )
        for row, (seed, pair) in enumerate(sorted(by_seed.items())):
            accuracy = axes[row, 2]
            for col, arm in enumerate(("normalized-mse", "physical-mae")):
                run_id, result, history = pair[arm]
                identity = result["identity"]
                ax = axes[row, col]
                steps = [v["step"] for v in history]
                for split, label, digest_key in (
                    ("fit_sample", "Fixed fit sample", "input_manifest_sha256"),
                    ("inner_validation", "Inner validation", "validation_manifest_sha256"),
                ):
                    ax.plot(steps, [v[split]["objective"] for v in history], marker=".", label=label)
                    for ev in history:
                        m = ev[split]
                        for metric, value in m.items():
                            if isinstance(value, list):
                                values = [(f"{metric}_h{h}", v, m["windows"]) for h, v in enumerate(value, 1)]
                            else:
                                count = (
                                    m["high_pairs"]
                                    if metric in ("high_mae", "high_persistence_mae")
                                    else m["pairs"]
                                )
                                if metric in (
                                    "pairs",
                                    "windows",
                                    "high_pairs",
                                    "high_station_hours",
                                    "high_blocks_168h",
                                ):
                                    count = ""
                                values = [(metric, value, count)]
                            for name, v, n in values:
                                out.writerow(
                                    [
                                        run_id,
                                        seed,
                                        arm,
                                        ev["step"],
                                        split,
                                        identity[digest_key],
                                        name,
                                        v,
                                        n,
                                        ev["step"] == result["best_step"],
                                        ev["step"] == result["step"],
                                    ]
                                )
                ax.axvline(result["best_step"], color="black", linestyle=":", label="Selected update")
                ax.axvline(result["step"], color="grey", linestyle="--", label="Stopping update")
                ax.set(
                    title=f"Seed {seed} · {arm}",
                    xlabel="Optimizer update",
                    ylabel="Normalized MSE" if col == 0 else "MAE (µg/m³)",
                )
                ax.legend(fontsize=8)
                for metric, style in (("mae", "-"), ("high_mae", "--")):
                    accuracy.plot(
                        steps,
                        [v["inner_validation"][metric] for v in history],
                        linestyle=style,
                        color=("tab:blue", "tab:orange")[col],
                        label=f"{arm} · {metric}",
                    )
            for metric, style in (("persistence_mae", ":"), ("high_persistence_mae", "-.")):
                baseline = history[0]["inner_validation"][metric]
                if baseline is not None:
                    accuracy.axhline(baseline, color="grey", linestyle=style, label=metric)
            accuracy.set(
                title=f"Seed {seed} · inner validation", xlabel="Optimizer update", ylabel="MAE (µg/m³)"
            )
            accuracy.legend(fontsize=7)
    figure.savefig(folder / "curves.png", dpi=160)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", action="append", required=True, help="MLflow run ID; repeat once per arm")
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    if args.output.exists():
        p.error("Choose a new report directory")
    from mlflow import MlflowClient

    client = MlflowClient()
    with TemporaryDirectory(dir=args.output.parent) as scratch:
        work = Path(scratch)
        records = []
        for run in args.run:
            with TemporaryDirectory(dir=work) as d:
                records.append(read_run(client, run, Path(d)))
        report = work / "report"
        report.mkdir()
        export(records, report)
        report.rename(args.output)


if __name__ == "__main__":
    main()
