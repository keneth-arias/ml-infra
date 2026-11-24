# SPDX-FileCopyrightText: 2025 The Qwen Team and The HuggingFace Inc. team
# SPDX-License-Identifier: Apache-2.0
# Fixed-shape DeltaNet and decoder paths adapted from Transformers 5.5.4.
"""Build and run the stateless Qwen PM2.5 forecast with OpenVINO."""

import argparse
import gc
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import MethodType

import numpy as np
import openvino as ov


def short_delta_rule(query, key, value, g, beta, initial_state, output_final_state, use_qk_l2norm_in_kernel):
    """Functional first-chunk DeltaNet for seven positions with zero initial state."""
    import torch
    from transformers.models.qwen3_5.modeling_qwen3_5 import l2norm

    if initial_state is not None or output_final_state or not use_qk_l2norm_in_kernel:
        raise ValueError("This export supports only stateless, normalized Qwen DeltaNet")
    if query.shape[1] != 7:
        raise ValueError("Expected six patch tokens and one readout token")

    dtype = query.dtype
    query, key, value, g, beta = [
        tensor.transpose(1, 2).contiguous().float()
        for tensor in (l2norm(query), l2norm(key), value, g, beta)
    ]
    query = query * (query.shape[-1] ** -0.5)
    g = g.cumsum(dim=-1)
    decay = (g.unsqueeze(-1) - g.unsqueeze(-2)).tril().exp().tril()
    key_transposed = key.transpose(-1, -2)
    lower = (-((key * beta.unsqueeze(-1)) @ key_transposed) * decay).tril(diagonal=-1)

    # The original chunk pads to 64. Only the leading 7x7 causal block can affect
    # the forecast. Rebuild its rows with concatenation for a mutation-free trace.
    rows = [lower[..., 0, :]]
    for index in range(1, 7):
        row = lower[..., index, :index]
        previous = torch.stack(rows, dim=-2)[..., :index]
        solved = row + (row.unsqueeze(-1) * previous).sum(-2)
        rows.append(torch.cat((solved, lower[..., index, index:]), dim=-1))
    transform = torch.stack(rows, dim=-2) + torch.eye(7, dtype=value.dtype, device=value.device)
    adjusted_values = transform @ (value * beta.unsqueeze(-1))
    attention = ((query @ key_transposed) * decay).tril()
    return (attention @ adjusted_values).transpose(1, 2).contiguous().to(dtype), None


def short_decoder_forward(decoder, *, inputs_embeds, use_cache):
    """Equivalent no-cache text path with a static causal mask for tracing."""
    import torch
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5ModelOutputWithPast

    if use_cache or inputs_embeds.shape[1] != 7:
        raise ValueError("Expected seven embeddings and disabled decoder cache")
    hidden = inputs_embeds
    positions = torch.arange(7, device=hidden.device).view(1, 7).expand(hidden.shape[0], -1)
    rotary = decoder.rotary_emb(hidden, positions.unsqueeze(0).expand(3, -1, -1))
    blocked = torch.ones(1, 1, 7, 7, dtype=torch.bool, device=hidden.device).triu(diagonal=1)
    causal = torch.zeros(1, 1, 7, 7, dtype=hidden.dtype, device=hidden.device).masked_fill(blocked, -math.inf)
    for kind, layer in zip(decoder.config.layer_types, decoder.layers, strict=True):
        hidden = layer(hidden, position_embeddings=rotary,
                       attention_mask=None if kind == "linear_attention" else causal,
                       position_ids=positions, past_key_values=None, use_cache=False)
    return Qwen3_5ModelOutputWithPast(last_hidden_state=decoder.norm(hidden))


def history_array(values):
    if not isinstance(values, list) or len(values) != 24 or any(
        type(value) not in (float, int) for value in values
    ):
        raise ValueError("Expected 24 hourly PM2.5 numbers in oldest-to-newest order")
    history = np.array(values, dtype=np.float32).reshape(1, 24)
    if not np.isfinite(history).all():
        raise ValueError("History must contain only finite FP32 values")
    return history


def run(compiled, history):
    forecasts = compiled([history])[compiled.output(0)]
    if forecasts.shape != (1, 6) or forecasts.dtype != np.float32 or not np.isfinite(forecasts).all():
        raise ValueError("OpenVINO produced invalid forecasts")
    return forecasts[0]


def compile_ir(directory, threads):
    core = ov.Core()
    model = core.read_model(directory / "model.xml")
    check_contract(model)
    return core.compile_model(model, "CPU", {
        "INFERENCE_PRECISION_HINT": ov.Type.f32,
        "INFERENCE_NUM_THREADS": threads,
        "NUM_STREAMS": 1,
    })


def check_contract(model):
    if (len(model.inputs) != 1 or len(model.outputs) != 1
            or list(model.input().shape) != [1, 24] or list(model.output().shape) != [1, 6]
            or model.input().get_element_type() != ov.Type.f32
            or model.output().get_element_type() != ov.Type.f32):
        raise ValueError("Expected a static FP32 [1, 24] to [1, 6] OpenVINO forecast graph")


def load_fixture(path):
    fixture = json.loads(path.read_text())
    histories = [history_array(values) for values in fixture["histories"]]
    expected = np.array(fixture["forecasts"], dtype=np.float32)
    if not histories or expected.shape != (len(histories), 6) or not np.isfinite(expected).all():
        raise ValueError("Fixture must provide six finite reference forecasts for every history")
    return histories, expected


def compare(actual, expected, atol, label):
    delta = np.abs(np.array(actual, dtype=np.float32) - expected)
    result = {"max_abs_ug_m3": float(delta.max()), "atol_ug_m3": atol, "cases": len(expected),
              "max_abs_by_horizon_ug_m3": delta.max(axis=0).tolist()}
    print(f"{label} {json.dumps(result, allow_nan=False)}", file=sys.stderr, flush=True)
    if not np.isfinite(delta).all() or np.any(delta > atol):
        raise ValueError(f"{label} exceeded absolute forecast tolerance {atol} µg/m³")
    return result


def convert(master, histories, expected, threads, atol):
    import torch
    from transformers.models.qwen3_5.modeling_qwen3_5 import Qwen3_5GatedDeltaNet, Qwen3_5TextModel
    from torch_forecast import load_bundle

    torch.set_num_threads(threads)
    model = load_bundle(master).cpu().eval()
    if next(model.parameters()).dtype != torch.float32:
        raise ValueError("Both OpenVINO artifacts require the FP32 master")
    cases = [torch.from_numpy(history) for history in histories]
    with torch.no_grad():
        compare([model(case).numpy()[0] for case in cases], expected, atol, "fp32_master")
        (decoder,) = [layer for layer in model.modules() if isinstance(layer, Qwen3_5TextModel)]
        delta_layers = [layer for layer in model.modules() if isinstance(layer, Qwen3_5GatedDeltaNet)]
        if len(delta_layers) != 18:
            raise ValueError(f"Expected 18 Qwen DeltaNet layers, found {len(delta_layers)}")
        for layer in delta_layers:
            layer.chunk_gated_delta_rule = short_delta_rule
        decoder.config._attn_implementation = "eager"
        decoder.forward = MethodType(short_decoder_forward, decoder)
        lowered = compare([model(case).numpy()[0] for case in cases], expected, atol, "fp32_lowered")
        traced = torch.jit.trace(model, (cases[0],), check_trace=False)
        graph = ov.convert_model(traced, example_input=cases[0], input=[ov.PartialShape([1, 24])], share_weights=False)
    check_contract(graph)
    graph.input().get_tensor().set_names({"history"})
    graph.output().get_tensor().set_names({"forecast"})
    return graph, lowered


def build(args):
    targets = [args.fp32.resolve(), args.fp16.resolve()]
    if targets[0] == targets[1] or any(target.exists() for target in targets):
        raise ValueError("FP32 and FP16 must be different new output directories")
    fixture = args.fixture or args.master / "reference.json"
    histories, expected = load_fixture(fixture)
    graph, lowered = convert(args.master, histories, expected, args.threads, args.fp32_atol)
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".openvino-fp32-", dir=targets[0].parent) as temp32, \
            TemporaryDirectory(prefix=".openvino-fp16-", dir=targets[1].parent) as temp16:
        directories = [Path(temp32), Path(temp16)]
        for directory, compressed in zip(directories, (False, True), strict=True):
            ov.save_model(graph, directory / "model.xml", compress_to_fp16=compressed)
        del graph
        gc.collect()

        for directory, dtype, atol in zip(directories, ("fp32", "fp16"),
                                         (args.fp32_atol, args.fp16_atol), strict=True):
            compiled = compile_ir(directory, args.threads)
            comparison = compare([run(compiled, history) for history in histories], expected, atol, "ir_" + dtype)
            report = {
                "openvino": ov.__version__, "weight_storage": dtype, "inference_precision_hint": "f32",
                "device": "CPU", "fp32_lowering": lowered, "compiled_ir": comparison,
                "bin_bytes": (directory / "model.bin").stat().st_size,
            }
            (directory / "openvino-validation.json").write_text(json.dumps(report, indent=2) + "\n")
            del compiled
        if (directories[1] / "model.bin").stat().st_size >= (directories[0] / "model.bin").stat().st_size:
            raise ValueError("FP16 compression did not reduce IR weights")
        for directory, target in zip(directories, targets, strict=True):
            directory.rename(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, default=4)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("master", type=Path)
    builder.add_argument("fp32", type=Path)
    builder.add_argument("fp16", type=Path)
    builder.add_argument("--fixture", type=Path)
    builder.add_argument("--fp32-atol", type=float, default=0.001)
    builder.add_argument("--fp16-atol", type=float, default=0.15)
    predictor = commands.add_parser("predict")
    predictor.add_argument("directory", type=Path)
    predictor.add_argument("history", type=Path)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be positive")
    if args.command == "build":
        if any(not math.isfinite(atol) or atol < 0 for atol in (args.fp32_atol, args.fp16_atol)):
            parser.error("Forecast tolerances must be finite and nonnegative")
        build(args)
    else:
        history = history_array(json.loads(args.history.read_text()))
        result = run(compile_ir(args.directory, args.threads), history)
        print(json.dumps(result.tolist(), allow_nan=False))


if __name__ == "__main__":
    main()
