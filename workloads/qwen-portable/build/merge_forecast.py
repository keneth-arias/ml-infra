"""Build the FP32 release master and its FP16 SafeTensors derivative."""

import argparse
import json
import shutil
import subprocess
from argparse import Namespace
from pathlib import Path

import torch
import torch.distributed.checkpoint as dcp
from peft.tuners.lora import LoraLayer
from safetensors.torch import save_file
from transformers import AutoTokenizer

from opentslm.forecast import QwenForecaster
from opentslm.forecast_checkpoint import files, read_manifest, sha256
from opentslm.forecast_inference import JointInputs, add_lora, forecast, load_checkpoint_model
from torch_forecast import PM25Forecast, load_bundle

CHECKPOINT = "ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf"
BASE_MANIFEST = "1d76cc3cfd4c15f9c749372611e5ca6e0b6f910b1fcf0551ce9157e9bfef29c1"
SCALER = "c8ebd87bce74ea0157e85093e4c9c81d981af34fb62f1cbc2c12ce653b2c3863"
BASE_REVISION = "2fc06364715b967f1860aea9cf38778875588b17"
TRAINER = "a4def7e420e2ecc271137bb3077341d39878d6c0"
TOLERANCE = {"fp32": 0.001, "fp16": 0.15}
CUSTOM = {"project.weight", "project.bias", "readout", "head.weight", "head.bias"}
TOKENIZER = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "chat_template.jinja")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n")


def verify_inputs(root):
    step = root / "step-00015745"
    scaler_file = root / "scaler-15745.json"
    manifest_file = root / "SHA256SUMS"
    if [sha256(path) for path in (step / "manifest.json", manifest_file, scaler_file)] != [
        CHECKPOINT, BASE_MANIFEST, SCALER
    ]:
        raise ValueError("Release input manifest or scaler hash mismatch")
    manifest = read_manifest(step)
    scaler = JointInputs.load(scaler_file, "first")
    identity = manifest["identity"]
    if (
        manifest["files"] != files(step)
        or manifest["step"] != 15745
        or identity["seed"] != 27
        or identity["model"] != BASE_REVISION
        or identity["trainer"] != TRAINER
        or identity["precision"] != "fp32"
        or identity["lora"] is not True
        or identity["head"] != "direct"
        or identity["joint"] != scaler.identity()
        or identity["weights"] != BASE_MANIFEST
    ):
        raise ValueError("Unexpected checkpoint files or release identity")
    listed = {line.split(maxsplit=1)[1].lstrip("*") for line in manifest_file.read_text().splitlines()}
    present = {str(path.relative_to(root)) for path in (root / "weights").rglob("*") if path.is_file()}
    if listed != present:
        raise ValueError("Base file inventory disagrees with the release")
    subprocess.run(["sha256sum", "--strict", "--check", manifest_file.name], cwd=root, check=True)
    return step, scaler, scaler_file


def histories():
    return torch.tensor([
        [0.0] * 24,
        [10.0] * 24,
        [3.0 + 0.65 * i for i in range(24)],
        [32.0 - 1.05 * i for i in range(24)],
        [11.0 + (i % 6) * 2.5 for i in range(24)],
        [18.0] * 18 + [23.0, 35.0, 61.0, 105.0, 153.0, 181.0],
        [24.0] * 10 + [90.0, 170.0, 130.0, 55.0] + [18.0] * 10,
        [36.0] * 20 + [82.0, 71.0, 40.0, 17.0],
        [110.0] * 24,
    ], dtype=torch.float32)


def check_forecasts(observed, reference, tolerance):
    if observed.shape != reference.shape or not observed.isfinite().all():
        raise ValueError("Expected finite six-horizon forecasts")
    error = (observed - reference).abs()
    maximum = error.max().item()
    if maximum > tolerance:
        raise ValueError(f"Maximum forecast difference {maximum:.7g} exceeds {tolerance}")
    return {"max_abs_ug_m3": maximum, "per_horizon_max_abs_ug_m3": error.amax(dim=0).tolist(),
            "tolerance_ug_m3": tolerance, "windows": len(reference)}


def copy_common(output, root, assets, source, scaler, dtype, reference):
    output.mkdir()
    write_json(output / "config.json", {
        "schema": "neblima-qwen-portable-v1",
        "dtype": {"fp32": "float32", "fp16": "float16"}[dtype],
        "model_type": "qwen3_5",
        "model": "Qwen3.5-0.8B",
        "seed": 27,
        "step": 15745,
        "sequence_length": 24,
        "patch_size": 4,
        "channels": ["pm25", "pm10"],
        "targets": ["pm25", "pm10"],
        "horizon": 6,
        "input_view": "first",
        "output_units": "µg/m³",
        "checkpoint_sha256": CHECKPOINT,
        "base_revision": BASE_REVISION,
        "base_manifest_sha256": BASE_MANIFEST,
        "scaler_sha256": SCALER,
        "training_source_revision": TRAINER,
    })
    write_json(output / "reference.json", reference)
    write_json(output / "example-history.json", reference["histories"][4])
    shutil.copyfile(root / "weights/config.json", output / "base-config.json")
    shutil.copyfile(scaler, output / "scaler.json")
    for filename in TOKENIZER:
        shutil.copyfile(assets / filename, output / filename)
    shutil.copyfile(assets / "LICENSE", output / "LICENSE-Qwen")
    shutil.copyfile(assets / "LICENSE-OpenTSLM", output / "LICENSE-OpenTSLM")
    shutil.copyfile(source / "torch_forecast.py", output / "torch_forecast.py")
    shutil.copyfile(source / "opentslm/forecast.py", output / "qwen_architecture.py")
    shutil.copyfile(source / "requirements-torch.txt", output / "requirements.txt")


@torch.inference_mode()
def build(args):
    torch.set_num_threads(args.threads)
    step, joint, scaler_path = verify_inputs(args.model_root)
    scaler = json.loads(scaler_path.read_text())
    model = QwenForecaster.from_pretrained(
        checkpoint_path=str(args.model_root / "weights"), local_files_only=True, channels=2, targets=2,
    ).eval()
    add_lora(model)
    model.eval()
    adapters = [(name, module) for name, module in model.named_modules() if isinstance(module, LoraLayer)]
    trainable = {name for name, param in model.named_parameters() if param.requires_grad}
    metadata = dcp.FileSystemReader(step / "dcp").read_metadata().state_dict_metadata
    checkpoint_keys = {name.removeprefix("model.") for name in metadata if name.startswith("model.")}
    if len(adapters) != 114 or len(trainable) != 233 or checkpoint_keys != trainable or not CUSTOM <= trainable:
        raise ValueError("Checkpoint does not contain the expected 114 LoRA pairs and five forecast tensors")
    load_checkpoint_model(model, step, True)
    print("Loaded the authenticated release DCP", flush=True)

    x = histories()
    raw = torch.stack((x, torch.zeros_like(x)), dim=-1)
    restored, center, scale = forecast(model, raw, Namespace(joint=joint, precision="fp32"))
    reference = (restored * scale + center)[..., 0]
    check_forecasts(PM25Forecast(model, scaler)(x), reference, TOLERANCE["fp32"])
    custom = {name: model.state_dict()[name].clone() for name in CUSTOM}
    for name, adapter in adapters:
        adapter.merge(safe_merge=True)
        model.set_submodule(name, adapter.get_base_layer(), strict=True)
    state = model.state_dict()
    if (
        any(isinstance(layer, LoraLayer) for layer in model.modules())
        or any("lora_" in name or ".base_layer." in name for name in state)
        or any(not torch.equal(state[name], tensor) for name, tensor in custom.items())
        or any(tensor.dtype != torch.float32 for tensor in state.values())
    ):
        raise ValueError("Merge left adapters or changed the forecast-specific tensors")
    merge_parity = check_forecasts(PM25Forecast(model, scaler)(x), reference, TOLERANCE["fp32"])
    print(f"FP32 merge parity {merge_parity}", flush=True)
    reference_record = {"checkpoint_sha256": CHECKPOINT, "histories": x.tolist(), "forecasts": reference.tolist()}
    tokenizer_examples = ["Lima PM2.5 forecast", "Pronóstico horario de calidad del aire"]
    pinned_tokenizer = AutoTokenizer.from_pretrained(args.assets, local_files_only=True)
    reference_ids = [pinned_tokenizer.encode(text) for text in tokenizer_examples]
    for dtype in ("fp32", "fp16"):
        target = args.output / f"safetensors-{dtype}"
        stage = args.output / f".safetensors-{dtype}.build"
        if target.exists() or stage.exists():
            raise FileExistsError(target)
        copy_common(stage, args.model_root, args.assets, args.source, scaler_path, dtype, reference_record)
        destination = stage / "model.safetensors"
        weights = state if dtype == "fp32" else {name: value.half() for name, value in state.items()}
        if any(not value.isfinite().all() for value in weights.values()):
            raise ValueError("Cannot export nonfinite weights")
        save_file(weights, destination, metadata={"format": "pt", "checkpoint_sha256": CHECKPOINT})
        del weights
        reloaded = load_bundle(stage)
        reload_parity = check_forecasts(reloaded(x), reference, TOLERANCE[dtype])
        del reloaded
        tokenizer = AutoTokenizer.from_pretrained(stage, local_files_only=True)
        if [tokenizer.encode(text) for text in tokenizer_examples] != reference_ids:
            raise ValueError("Tokenizer changed during packaging")
        report = {
            "runtime": "pytorch",
            "torch": torch.__version__,
            "weight_dtype": dtype,
            "compute_dtype": "fp32",
            "tensors": len(state),
            "scalars": sum(tensor.numel() for tensor in state.values()),
            "weights_bytes": destination.stat().st_size,
            "weights_sha256": sha256(destination),
            "fp32_merge_parity": merge_parity,
            "offline_reload_parity": reload_parity,
            "tokenizer_probes": dict(zip(tokenizer_examples, reference_ids, strict=True)),
        }
        write_json(stage / "build-receipt.json", report)
        stage.rename(target)
        print(f"Verified {target.name}: {reload_parity}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
