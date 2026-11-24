"""Test a packaged Neblima Qwen MLX export on macOS Metal."""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import platform
import resource
import statistics
import time
from pathlib import Path

import mlx.core as mx


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def error(observed, expected):
    differences = []
    for output, reference in zip(observed, expected, strict=True):
        pairs = list(zip(output, reference, strict=True))
        if len(pairs) != 6 or any(not math.isfinite(value) for pair in pairs for value in pair):
            raise ValueError("Expected six finite forecasts and references per window")
        differences.append([abs(a - b) for a, b in pairs])
    return [max(horizon) for horizon in zip(*differences, strict=True)]


def benchmark(model, history, repeats=5):
    durations = []
    for _ in range(repeats):
        started = time.perf_counter()
        model.predict(history).tolist()
        durations.append(time.perf_counter() - started)
    return {"median_seconds": statistics.median(durations), "max_seconds": max(durations)}


def check(bundle, weight_pin):
    if platform.system() != "Darwin" or platform.machine() != "arm64" or not mx.metal.is_available():
        raise RuntimeError("This test requires native Apple Silicon Metal")
    weights = digest(bundle / "model.safetensors")
    if weights != weight_pin:
        raise ValueError("Weights disagree with the independent release pin")
    spec = importlib.util.spec_from_file_location("portable_forecaster", bundle / "mlx_forecast.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    fixture_path = bundle / "reference.json"
    fixture = json.loads(fixture_path.read_text())
    config = json.loads((bundle / "config.json").read_text())
    tolerance = {"float32": 0.001, "float16": 0.15}[config["dtype"]]
    if config["checkpoint_sha256"] != fixture["checkpoint_sha256"] or len(fixture["histories"]) != 9:
        raise ValueError("Expected the released checkpoint and nine histories")
    mx.set_default_device(mx.gpu)
    mx.reset_peak_memory()
    started = time.perf_counter()
    model = runner.load_bundle(bundle)
    load_seconds = time.perf_counter() - started
    history = fixture["histories"]
    expected = fixture["forecasts"]
    started = time.perf_counter()
    first = model.predict(history[0]).tolist()
    cold_seconds = time.perf_counter() - started
    single = [first] + [model.predict(window).tolist() for window in history[1:]]
    batched = model.predict(history).tolist()
    per_horizon = {
        "single": error(single, expected),
        "batch_9": error(batched, expected),
    }
    maximum = max(value for errors in per_horizon.values() for value in errors)
    if maximum > tolerance:
        raise ValueError(f"Forecast drift {maximum:.7g} exceeds {tolerance} µg/m³")
    warm = {
        "single": benchmark(model, history[0]),
        "batch_9": benchmark(model, history),
        "batch_16": benchmark(model, [history[index % 9] for index in range(16)]),
    }
    for invalid in (history[0][:-1], [float("nan")] + history[0][1:]):
        try:
            model.predict(invalid)
        except ValueError:
            continue
        raise ValueError("Malformed history was accepted")
    return {
        "schema": "neblima-portable-metal-check-v1",
        "bundle": bundle.name,
        "checkpoint_sha256": config["checkpoint_sha256"],
        "weights_sha256": weights,
        "reference_sha256": digest(fixture_path),
        "runner_sha256": digest(bundle / "mlx_forecast.py"),
        "storage_dtype": config["dtype"],
        "compute_dtype": "float32",
        "runtime": {"os": platform.mac_ver()[0], "machine": platform.machine(),
                    "python": platform.python_version(), "mlx": importlib.metadata.version("mlx"),
                    "device": mx.device_info(mx.gpu)["device_name"]},
        "histories": len(history),
        "horizon": 6,
        "absolute_tolerance_ug_m3": tolerance,
        "max_abs_ug_m3": maximum,
        "per_horizon_max_abs_ug_m3": per_horizon,
        "load_seconds": load_seconds,
        "first_forward_seconds": cold_seconds,
        "warm": warm,
        "mlx_peak_bytes": mx.get_peak_memory(),
        "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("weights_sha256")
    args = parser.parse_args()
    print(json.dumps(check(args.bundle, args.weights_sha256), indent=2, allow_nan=False))
