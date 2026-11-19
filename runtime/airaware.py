"""Run the verified AirAware training workload and track each attempt."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def window_inputs(root, version):
    if version == "pilot":
        return root, {}
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", version):
        raise ValueError("Expected a lowercase input version")
    bundle = root / "windows" / version
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    if manifest["version"] != version or manifest["timef_manifest_sha256"] != sha256(root / "timef/manifest.json"):
        raise ValueError("Input bundle does not match the selected TimeF release")
    hashes = {}
    for split in ("train", "validation"):
        digest = sha256(bundle / f"{split}.npz")
        if digest != manifest["inputs"][split]["npz_sha256"]:
            raise ValueError(f"Corrupt {split} input")
        hashes[f"{split}_sha256"] = digest
    return bundle, {**hashes, "input_manifest_sha256": sha256(path)}


def main():
    import mlflow

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--mode", choices=("single", "ddp", "fsdp"), default="fsdp")
    parser.add_argument("--input-version", default="pilot")
    options, trainer_args = parser.parse_known_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", options.run_id):
        parser.error("Expected a lowercase run identifier")
    inputs = Path("/inputs")
    subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=inputs, check=True)
    subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=inputs / "qwen", check=True)
    subprocess.run(
        ["/opt/reader/.venv/bin/python", "/opt/reader/qwen_window.py", inputs / "timef"],
        env={**os.environ, "PYTHONPATH": ""}, stdout=subprocess.DEVNULL, check=True,
    )
    windows, input_hashes = window_inputs(inputs, options.input_version)
    process_count = 1 if options.mode == "single" else 2
    command = [
        "/opt/conda/bin/torchrun", "--standalone", f"--nproc-per-node={process_count}",
        "/opt/forecast/train_qwen_forecast.py", "--mode", options.mode,
        "--data", str(windows / "train.npz"), "--validation", str(windows / "validation.npz"),
        "--weights", str(inputs / "qwen/weights"), "--run-dir", f"/checkpoints/{options.run_id}",
        *trainer_args,
    ]
    mlflow.set_experiment("airaware-pm25")
    with mlflow.start_run(run_name=options.run_id):
        mlflow.log_params({
            "run_id": options.run_id, "mode": options.mode, "ranks": process_count,
            "input_version": options.input_version, **input_hashes,
            "arguments": " ".join(trainer_args), "forecast_commit": os.environ["FORECAST_SOURCE_REVISION"],
            "attempt": os.environ.get("SKYPILOT_TASK_ID", "local"),
        })
        with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": "/opt/forecast/src"}) as process:
            for line in process.stdout:
                print(line, end="", flush=True)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if "checkpoint_bytes" in event:
                    checkpoint = event["checkpoint"]
                    artifact = Path("/checkpoints") / options.run_id / checkpoint / "manifest.json"
                    mlflow.log_artifact(str(artifact), artifact_path=checkpoint)
                    mlflow.log_metrics({key: event[key] for key in ("checkpoint_bytes", "checkpoint_seconds")},
                                       step=int(checkpoint.removeprefix("step-")))
                if "step" in event:
                    metrics = {key: value for key, value in event.items()
                               if key != "step" and isinstance(value, (int, float))}
                    metrics.update({f"mae_h{h}": value for h, value in enumerate(event.get("mae_by_horizon", []), 1)})
                    mlflow.log_metrics(metrics, step=event["step"])
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, command)


if __name__ == "__main__":
    main()
