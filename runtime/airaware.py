"""Run the verified AirAware training workload and track each attempt."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

import mlflow


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--mode", choices=("single", "ddp", "fsdp"), default="fsdp")
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
    process_count = 1 if options.mode == "single" else 2
    command = [
        "/opt/conda/bin/torchrun", "--standalone", f"--nproc-per-node={process_count}",
        "/opt/forecast/train_qwen_forecast.py", "--mode", options.mode,
        "--data", str(inputs / "train.npz"), "--validation", str(inputs / "validation.npz"),
        "--weights", str(inputs / "qwen/weights"), "--run-dir", f"/checkpoints/{options.run_id}",
        *trainer_args,
    ]
    mlflow.set_experiment("airaware-pm25")
    with mlflow.start_run(run_name=options.run_id):
        mlflow.log_params({
            "run_id": options.run_id, "mode": options.mode, "ranks": process_count,
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
