"""Run the Neblima six-hour numerical forecaster from merged local weights."""

import argparse
import json
from pathlib import Path

import torch
from safetensors.torch import load_file
from torch import nn
from transformers import Qwen3_5TextConfig, Qwen3_5TextModel

from qwen_architecture import QwenForecaster


class PM25Forecast(nn.Module):
    def __init__(self, model, scaler):
        super().__init__()
        self.model = model
        self.register_buffer("center", torch.tensor(scaler["mean"][0], dtype=torch.float32))
        self.register_buffer("scale", torch.tensor(scaler["scale"][0], dtype=torch.float32))

    def forward(self, history):
        normalized = (history.float() - self.center) / self.scale
        joint = torch.stack((normalized, torch.zeros_like(normalized)), dim=-1)
        joint = joint.to(self.model.project.weight.dtype)
        observed = torch.stack(
            (torch.ones_like(history, dtype=torch.bool), torch.zeros_like(history, dtype=torch.bool)),
            dim=-1,
        )
        return self.model(joint, observed)[..., 0].float() * self.scale + self.center


def load_bundle(path, *, compute="fp32"):
    bundle = Path(path)
    config = json.loads((bundle / "config.json").read_text())
    if (
        config["schema"] != "neblima-qwen-portable-v1"
        or config["sequence_length"] != 24
        or config["channels"] != ["pm25", "pm10"]
        or config["targets"] != ["pm25", "pm10"]
        or config["input_view"] != "first"
        or config["horizon"] != 6
        or config["patch_size"] != 4
    ):
        raise ValueError("Unexpected Neblima forecast configuration")
    text_config = Qwen3_5TextConfig.from_dict(
        json.loads((bundle / "base-config.json").read_text())["text_config"]
    )
    text_config._attn_implementation = "sdpa"
    model = QwenForecaster(Qwen3_5TextModel(text_config), channels=2, targets=2)
    weights = load_file(bundle / "model.safetensors")
    dtype = {"float32": torch.float32, "float16": torch.float16}[config["dtype"]]
    if any(tensor.dtype != dtype for tensor in weights.values()):
        raise ValueError("Stored tensor dtype disagrees with the manifest")
    model.load_state_dict(weights, strict=True, assign=True)
    if compute == "fp32":
        model.float()
    elif compute != "stored":
        raise ValueError("Unknown compute precision")
    scaler = json.loads((bundle / "scaler.json").read_text())
    return PM25Forecast(model, scaler).eval()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("history", type=Path, help="JSON array of 24 hourly PM2.5 concentrations")
    parser.add_argument("--compute", choices=("fp32", "stored"), default="fp32")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    values = torch.tensor(json.loads(args.history.read_text()), dtype=torch.float32)
    if values.shape != (24,) or not values.isfinite().all():
        parser.error("Expected exactly 24 finite hourly PM2.5 concentrations")
    torch.set_num_threads(args.threads)
    with torch.inference_mode():
        result = load_bundle(args.bundle, compute=args.compute)(values.unsqueeze(0))[0]
    print(json.dumps(result.tolist(), allow_nan=False))


if __name__ == "__main__":
    main()
