"""Build the MLX packages and add portable metadata to the OpenVINO IRs."""

import argparse
import hashlib
import json
import shutil
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

COMMON = (
    "base-config.json", "scaler.json", "reference.json", "example-history.json",
    "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
    "chat_template.jinja", "LICENSE-Qwen", "LICENSE-OpenTSLM",
)
TOLERANCE = {"fp32": 0.001, "fp16": 0.15}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")


def stage_metadata(source, target, backend):
    for name in COMMON:
        shutil.copyfile(source / name, target / name)
    config = json.loads((source / "config.json").read_text())
    config["backend"] = backend
    write_json(target / "config.json", config)


def package_mlx(output, dtype):
    import mlx.core as mx
    import numpy as np

    from mlx_forecast import load_bundle

    source = output / f"safetensors-{dtype}"
    target = output / f"mlx-{dtype}"
    if target.exists():
        raise FileExistsError(target)
    with TemporaryDirectory(dir=output, prefix=f".mlx-{dtype}-") as temporary:
        stage = Path(temporary)
        stage_metadata(source, stage, "mlx")
        shutil.copyfile(Path(__file__).with_name("mlx_forecast.py"), stage / "mlx_forecast.py")
        (stage / "requirements.txt").write_text(
            'mlx==0.32.4; platform_system == "Darwin"\n'
            'mlx[cpu]==0.32.4; platform_system == "Linux"\n'
        )
        mx.set_default_device(mx.cpu)
        weight_path = stage / "model.safetensors"
        tensors = mx.load(str(source / "model.safetensors"))
        mx.save_safetensors(str(weight_path), tensors, metadata={"format": "mlx"})
        del tensors
        fixture = json.loads((stage / "reference.json").read_text())
        model = load_bundle(stage)
        result = np.array([
            model.predict(history).tolist() for history in fixture["histories"]
        ], dtype=np.float32)
        expected = np.array(fixture["forecasts"], dtype=np.float32)
        error = np.abs(result - expected)
        maximum = float(error.max())
        if result.shape != expected.shape or not np.isfinite(error).all() or maximum > TOLERANCE[dtype]:
            raise ValueError(f"MLX {dtype} maximum forecast difference {maximum:.7g} exceeds {TOLERANCE[dtype]}")
        receipt = {
            "runtime": "mlx", "version": version("mlx"), "device": "linux-x86_64-cpu",
            "weight_dtype": dtype,
            "compute_dtype": "fp32",
            "weights_bytes": weight_path.stat().st_size, "weights_sha256": sha256(weight_path),
            "source_weights_sha256": json.loads((source / "build-receipt.json").read_text())["weights_sha256"],
            "offline_reload_parity": {
                "max_abs_ug_m3": maximum, "per_horizon_max_abs_ug_m3": error.max(axis=0).tolist(),
                "tolerance_ug_m3": TOLERANCE[dtype], "windows": len(expected),
            },
        }
        write_json(stage / "build-receipt.json", receipt)
        stage.rename(target)
        print(f"Verified {target.name}: max drift {maximum:.7g} µg/m³", flush=True)


def package_openvino(output, dtype):
    target = output / f"openvino-{dtype}"
    if not (target / "openvino-validation.json").is_file():
        raise ValueError("Offline OpenVINO verification report is missing")
    stage_metadata(output / f"safetensors-{dtype}", target, "openvino")
    shutil.copyfile(Path(__file__).with_name("openvino_forecast.py"), target / "openvino_forecast.py")
    (target / "requirements.txt").write_text("openvino==2026.4.1\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=("mlx", "openvino"))
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    for dtype in ("fp32", "fp16"):
        {"mlx": package_mlx, "openvino": package_openvino}[args.backend](args.output, dtype)


if __name__ == "__main__":
    main()
