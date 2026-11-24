# SPDX-License-Identifier: Apache-2.0
"""Offline MLX runner for the fixed 24-hour Neblima Qwen forecast.

Implements the text-only decoder operations of the Qwen Team and Hugging Face
Transformers v5.5.4 for numeric embeddings, with zero initial recurrent state.
The forecast and weights use the original OpenTSLM tensor names.

Reference decoder: https://github.com/huggingface/transformers/blob/v5.5.4/src/transformers/models/qwen3_5/modeling_qwen3_5.py
"""

import argparse
import json
import math
import sys
from pathlib import Path

import mlx.core as mx


def silu(x):
    return x * mx.sigmoid(x)


def rms(x, epsilon):
    return x * mx.rsqrt(mx.mean(mx.square(x), axis=-1, keepdims=True) + epsilon)


def rotate(x, angle, dtype):
    rotary_width = angle.shape[-1]
    left, right = mx.split(x[..., :rotary_width], 2, axis=-1)
    rotated = mx.concatenate((-right, left), axis=-1)
    cos, sin = mx.cos(angle).astype(dtype), mx.sin(angle).astype(dtype)
    return mx.concatenate((x[..., :rotary_width] * cos + rotated * sin, x[..., rotary_width:]), axis=-1)


class Forecaster:
    def __init__(self, bundle):
        bundle = Path(bundle)
        config = json.loads((bundle / "config.json").read_text())
        contract = {
            "schema": "neblima-qwen-portable-v1",
            "sequence_length": 24,
            "patch_size": 4,
            "channels": ["pm25", "pm10"],
            "targets": ["pm25", "pm10"],
            "horizon": 6,
            "input_view": "first",
        }
        if any(config.get(key) != value for key, value in contract.items()):
            raise ValueError("Unsupported numeric forecast contract")
        storage_dtype = {"float32": mx.float32, "float16": mx.float16}[config["dtype"]]
        self.args = json.loads((bundle / "base-config.json").read_text())["text_config"]
        if (self.args["model_type"] != "qwen3_5_text" or self.args["attention_bias"]
                or not self.args["attn_output_gate"] or self.args["hidden_act"] != "silu"
                or self.args["rope_parameters"]["rope_type"] != "default"):
            raise ValueError("Unsupported Qwen decoder configuration")
        scaler = json.loads((bundle / "scaler.json").read_text())
        if scaler["channels"] != contract["channels"]:
            raise ValueError("Scaler channel order does not match the forecast model")
        self.center, self.scale = scaler["mean"][0], scaler["scale"][0]
        if not math.isfinite(self.center) or not math.isfinite(self.scale) or self.scale <= 0:
            raise ValueError("Invalid PM2.5 scaler")
        self.weights = mx.load(str(bundle / "model.safetensors"))
        if any(tensor.dtype != storage_dtype for tensor in self.weights.values()):
            raise ValueError("Unexpected weight dtype in merged model")

    def linear(self, x, name, bias=False):
        weight = self.weights[name + ".weight"].astype(x.dtype)
        output = x @ weight.T
        return output + self.weights[name + ".bias"].astype(x.dtype) if bias else output

    def norm(self, x, name):
        scale = 1 + self.weights[name + ".weight"].astype(mx.float32)
        return (rms(x.astype(mx.float32), self.args["rms_norm_eps"]) * scale).astype(x.dtype)

    def attention(self, x, prefix, angle, causal_mask):
        batch, length, _ = x.shape
        head_size = self.args["head_dim"]
        heads, kv_heads = self.args["num_attention_heads"], self.args["num_key_value_heads"]
        query_gate = self.linear(x, prefix + ".q_proj").reshape(batch, length, heads, 2 * head_size)
        query, gate = mx.split(query_gate, 2, axis=-1)
        key = self.linear(x, prefix + ".k_proj").reshape(batch, length, kv_heads, head_size)
        value = self.linear(x, prefix + ".v_proj").reshape(batch, length, kv_heads, head_size)
        query = rotate(self.norm(query, prefix + ".q_norm").transpose(0, 2, 1, 3), angle, x.dtype)
        key = rotate(self.norm(key, prefix + ".k_norm").transpose(0, 2, 1, 3), angle, x.dtype)
        key = mx.repeat(key, heads // kv_heads, axis=1)
        value = mx.repeat(value.transpose(0, 2, 1, 3), heads // kv_heads, axis=1)
        scores = (query.astype(mx.float32) @ key.astype(mx.float32).swapaxes(-1, -2)) * head_size**-0.5
        scores = mx.where(causal_mask, scores, -math.inf)
        output = (mx.softmax(scores, axis=-1) @ value.astype(mx.float32)).astype(x.dtype).transpose(0, 2, 1, 3)
        gated = output * mx.sigmoid(gate)
        return self.linear(gated.reshape(batch, length, heads * head_size), prefix + ".o_proj")

    def delta_net(self, x, prefix):
        batch, length, _ = x.shape
        key_heads, value_heads = self.args["linear_num_key_heads"], self.args["linear_num_value_heads"]
        key_size, value_size = self.args["linear_key_head_dim"], self.args["linear_value_head_dim"]
        key_width = key_heads * key_size
        conv_weight = self.weights[prefix + ".conv1d.weight"].squeeze(1).astype(mx.float32)
        kernel_width = conv_weight.shape[-1]
        qkv = self.linear(x, prefix + ".in_proj_qkv").astype(mx.float32)
        padded = mx.pad(qkv, [(0, 0), (kernel_width - 1, 0), (0, 0)])
        convolved = mx.zeros_like(qkv)
        for offset in range(kernel_width):
            convolved = convolved + padded[:, offset:offset + length, :] * conv_weight[:, offset]
        query, key, value = mx.split(silu(convolved), [key_width, 2 * key_width], axis=-1)
        query = query.reshape(batch, length, key_heads, key_size)
        key = key.reshape(batch, length, key_heads, key_size)
        value = value.reshape(batch, length, value_heads, value_size)
        query = query * mx.rsqrt(mx.sum(mx.square(query), axis=-1, keepdims=True) + 1e-6) * key_size**-0.5
        key = key * mx.rsqrt(mx.sum(mx.square(key), axis=-1, keepdims=True) + 1e-6)
        if key_heads != value_heads:
            query = mx.repeat(query, value_heads // key_heads, axis=2)
            key = mx.repeat(key, value_heads // key_heads, axis=2)
        beta = mx.sigmoid(self.linear(x, prefix + ".in_proj_b").astype(mx.float32))
        a = self.linear(x, prefix + ".in_proj_a").astype(mx.float32)
        dt_bias = self.weights[prefix + ".dt_bias"].astype(mx.float32)
        decay = mx.exp(-mx.exp(self.weights[prefix + ".A_log"].astype(mx.float32)) * mx.logaddexp(a + dt_bias, 0))

        memory = mx.zeros((batch, value_heads, key_size, value_size), dtype=mx.float32)
        result = []
        for i in range(length):
            k = key[:, i, :, :, None]
            memory = memory * decay[:, i, :, None, None]
            change = (value[:, i] - mx.sum(memory * k, axis=-2)) * beta[:, i, :, None]
            memory = memory + k * change[:, :, None, :]
            result.append(mx.sum(memory * query[:, i, :, :, None], axis=-2))
        result = mx.stack(result, axis=1)
        gate = self.linear(x, prefix + ".in_proj_z").reshape(batch, length, value_heads, value_size)
        scale = self.weights[prefix + ".norm.weight"].astype(mx.float32)
        output = rms(result, self.args["rms_norm_eps"]) * scale * silu(gate.astype(mx.float32))
        output = output.reshape(batch, length, value_heads * value_size).astype(x.dtype)
        return self.linear(output, prefix + ".out_proj")

    def predict(self, history, *, verbose=False):
        """Accept one 24-hour window or a batch of windows in physical units."""
        history = mx.array(history, dtype=mx.float32)
        unbatched = history.ndim == 1
        if unbatched:
            history = history[None, :]
        if (
            history.ndim != 2 or history.shape[1] != 24 or not history.shape[0]
            or not mx.all(mx.isfinite(history)).item()
        ):
            raise ValueError("Expected at least one history of 24 finite hourly PM2.5 readings")
        batch = history.shape[0]
        center = (history - self.center) / self.scale
        zeros = mx.zeros_like(center)
        features = mx.stack((center, zeros, mx.ones_like(center), zeros), axis=-1)
        tokens = self.linear(features.reshape(batch, 6, 16), "project", bias=True)
        readout = mx.broadcast_to(self.weights["readout"].astype(tokens.dtype), (batch, 1, tokens.shape[-1]))
        x = mx.concatenate((tokens, readout), axis=1)
        positions = mx.arange(7)
        causal_mask = positions[:, None] >= positions[None, :]
        rope = self.args["rope_parameters"]
        width = int(self.args["head_dim"] * rope["partial_rotary_factor"])
        frequencies = rope["rope_theta"] ** (-mx.arange(0, width, 2, dtype=mx.float32) / width)
        # Equal position numbers along all three MRoPE axes reduce to ordinary RoPE.
        phase = positions[:, None] * frequencies[None, :]
        angle = mx.concatenate((phase, phase), axis=-1)

        for i, kind in enumerate(self.args["layer_types"]):
            layer = f"decoder.layers.{i}"
            state = self.norm(x, layer + ".input_layernorm")
            if kind == "full_attention":
                x = x + self.attention(state, layer + ".self_attn", angle, causal_mask)
            elif kind == "linear_attention":
                x = x + self.delta_net(state, layer + ".linear_attn")
            else:
                raise ValueError(f"Unsupported decoder layer {kind}")
            state = self.norm(x, layer + ".post_attention_layernorm")
            gate = self.linear(state, layer + ".mlp.gate_proj")
            up = self.linear(state, layer + ".mlp.up_proj")
            x = x + self.linear(silu(gate) * up, layer + ".mlp.down_proj")
            mx.eval(x)
            if verbose:
                print(f"MLX layer {i + 1}/{len(self.args['layer_types'])}", file=sys.stderr)

        x = self.norm(x[:, -1], "decoder.norm")
        joint = self.linear(x, "head", bias=True).reshape(batch, 6, 2).astype(mx.float32)
        prediction = joint[:, :, 0] * self.scale + self.center
        if not mx.all(mx.isfinite(prediction)).item():
            raise ValueError("Nonfinite PM2.5 forecast")
        return prediction[0] if unbatched else prediction


def load_bundle(bundle):
    return Forecaster(bundle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("history_json", type=Path)
    parser.add_argument("--device", choices=("cpu", "gpu"), default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if args.device:
        mx.set_default_device(getattr(mx, args.device))
    history = json.loads(args.history_json.read_text())
    prediction = load_bundle(args.bundle).predict(history, verbose=args.verbose)
    print(json.dumps(prediction.tolist(), allow_nan=False))


if __name__ == "__main__":
    main()
