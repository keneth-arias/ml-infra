"""Check the six built packages and inventory the exact offline deliverables."""

import argparse
import hashlib
import json
import platform
import struct
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from package_exports import COMMON

BACKENDS = ("safetensors", "mlx", "openvino")
TYPE = {"fp32": ("F32", "f32", "float32"), "fp16": ("F16", "f16", "float16")}
CHECKPOINT = "ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf"
CUSTOM = {"project.weight", "project.bias", "readout", "head.weight", "head.bias"}
CLI = {
    "safetensors": ("torch_forecast.py",),
    "mlx": ("mlx_forecast.py",),
    "openvino": ("openvino_forecast.py", "predict"),
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_weights(folder, backend, dtype):
    if backend == "openvino":
        data = folder / "model.bin"
        xml = ElementTree.parse(folder / "model.xml").getroot()
        stored = {entry.get("element_type") for entry in xml.findall(".//layer[@type='Const']/data")}
        if TYPE[dtype][1] not in stored or (dtype == "fp32" and "f16" in stored):
            raise ValueError(f"Unexpected OpenVINO constant types: {sorted(stored)}")
        result = {"stored_constant_types": sorted(stored)}
    else:
        data = folder / "model.safetensors"
        with data.open("rb") as stream:
            header_size, = struct.unpack("<Q", stream.read(8))
            header = json.loads(stream.read(header_size))
        metadata = header.pop("__metadata__")
        if backend == "mlx" and metadata["format"] != "mlx":
            raise ValueError("Expected the MLX runtime's SafeTensors serialization")
        if len(header) != 325 or not CUSTOM <= set(header):
            raise ValueError("The complete merged forecast tensor inventory is required")
        if any("lora_" in name or entry["dtype"] != TYPE[dtype][0] for name, entry in header.items()):
            raise ValueError("All weights must be merged and use the stored precision")
        result = {"tensors": len(header), "metadata": metadata}
    return {"file": data.name, "size_bytes": data.stat().st_size, "sha256": sha256(data), **result}


def write_readme(folder, backend, dtype, error):
    cli = " ".join(CLI[backend])
    (folder / "README.md").write_text(
        f"# Neblima Qwen3.5-0.8B · {backend} {dtype.upper()}\n\n"
        "This bundle contains the merged Seed 27, step 15,745 forecaster. Feed it 24 consecutive hourly "
        "PM2.5 concentrations in µg/m³, oldest first. It returns the next six hourly forecasts in µg/m³. "
        "The upstream tokenizer files are included for completeness. The forecaster takes numbers directly.\n\n"
        f"Stored weights use {dtype.upper()}. The verified runner computes in FP32.\n\n"
        "Install the pinned runtime in a clean Python 3.11 environment, then run the example. Once the "
        "dependencies are installed, the command runs with local files.\n\n"
        "```sh\n"
        "python -m pip install -r requirements.txt\n"
        f"python {cli} . example-history.json\n"
        "```\n\n"
        f"The maximum absolute difference across nine test histories and all six horizons was "
        f"{error:.8g} µg/m³ on Linux x86-64 CPU. See `reference.json` for the test inputs and "
        "`SHA256SUMS` for the file inventory. On macOS use `shasum -a 256 -c SHA256SUMS`. "
        "On Linux use `sha256sum -c SHA256SUMS`.\n"
    )


def verify(root):
    exports = {}
    master = root / "safetensors-fp32"
    pinned_shared = {name: sha256(master / name) for name in COMMON}
    for backend in BACKENDS:
        for dtype, tolerance in (("fp32", 0.001), ("fp16", 0.15)):
            name = f"{backend}-{dtype}"
            folder = root / name
            config = json.loads((folder / "config.json").read_text())
            if config["checkpoint_sha256"] != CHECKPOINT or config["dtype"] != TYPE[dtype][2]:
                raise ValueError(f"Unexpected config in {name}")
            if any(sha256(folder / file) != digest for file, digest in pinned_shared.items()):
                raise ValueError(f"Changed reference, scaler, tokenizer or license in {name}")
            weights = verify_weights(folder, backend, dtype)
            if backend == "openvino":
                build_report = json.loads((folder / "openvino-validation.json").read_text())
                parity = build_report["compiled_ir"]
            else:
                build_report = json.loads((folder / "build-receipt.json").read_text())
                parity = build_report["offline_reload_parity"]
                if build_report["weights_sha256"] != weights["sha256"]:
                    raise ValueError("Weights changed after the original runtime verification")
            command = [sys.executable, *CLI[backend], ".", "example-history.json"]
            forecast = json.loads(subprocess.check_output(command, cwd=folder, text=True))
            ref = json.loads((folder / "reference.json").read_text())["forecasts"][4]
            example_error = max(abs(value - expected) for value, expected in zip(forecast, ref, strict=True))
            if not 0 <= example_error <= tolerance or not 0 <= parity["max_abs_ug_m3"] <= tolerance:
                raise ValueError(f"Published CLI output drifted for {name}")
            write_readme(folder, backend, dtype, parity["max_abs_ug_m3"])
            files = {
                path.name: {"sha256": sha256(path), "bytes": path.stat().st_size}
                for path in sorted(folder.iterdir())
                if path.is_file() and path.name != "SHA256SUMS"
            }
            (folder / "SHA256SUMS").write_text(
                "".join(f"{file['sha256']}  {filename}\n" for filename, file in files.items())
            )
            exports[name] = {
                "weights": weights, "storage": dtype, "compute": "fp32", "files": files,
                "package_bytes": sum(item["bytes"] for item in files.values()),
                "max_abs_ug_m3": parity["max_abs_ug_m3"], "limit_ug_m3": tolerance,
                "standalone_cli_max_abs_ug_m3": example_error,
            }
            print(f"{name}: standalone CLI and {len(files)} files verified", flush=True)
    (root / "release.json").write_text(json.dumps({
        "schema": "neblima-qwen-portable-build-v1",
        "checkpoint_sha256": CHECKPOINT,
        "reference_weights_sha256": exports["safetensors-fp32"]["weights"]["sha256"],
        "test_platform": f"{platform.system()} {platform.machine()} CPU, bm MicroK8s",
        "synthetic_histories": 9,
        "packages": exports,
    }, indent=2, sort_keys=True, allow_nan=False) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exports", type=Path)
    verify(parser.parse_args().exports)
