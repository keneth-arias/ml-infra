"""Offline dual-rank LoRA fixture. Run with accelerate launch."""
import argparse
import tempfile
import time
from pathlib import Path

import mlflow
import torch
import torch.distributed as dist
from accelerate import Accelerator
from accelerate.utils import DistributedType, set_seed
from peft import LoraConfig, get_peft_model
from transformers import LlamaConfig, LlamaForCausalLM

parser = argparse.ArgumentParser()
parser.add_argument("--checkpoint", required=True)
parser.add_argument("--resume", action="store_true")
args = parser.parse_args()
acc = Accelerator()
set_seed(42)
model = get_peft_model(LlamaForCausalLM(LlamaConfig(
    vocab_size=256, hidden_size=128, intermediate_size=256,
    num_hidden_layers=2, num_attention_heads=4, num_key_value_heads=2,
)), LoraConfig(r=4, target_modules=["q_proj", "v_proj"], task_type="CAUSAL_LM"))
optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=1e-3)
model, optimizer = acc.prepare(model, optimizer)
checkpoint = Path(args.checkpoint)
if args.resume:
    acc.load_state(str(checkpoint))
if acc.is_main_process:
    mlflow.set_experiment("microk8s-smoke")
    mlflow.start_run()
    mlflow.log_params(dict(world_size=acc.num_processes, gpu=torch.cuda.get_device_name(), torch=torch.__version__, mode=str(acc.distributed_type), resumed=args.resume))
value = torch.tensor(acc.process_index + 1, device=acc.device)
if acc.num_processes > 1:
    dist.all_reduce(value)
assert value.item() == acc.num_processes * (acc.num_processes + 1) // 2
torch.cuda.synchronize()
started = time.monotonic()
for step in range(2):
    tokens = torch.randint(0, 256, (2, 32), device=acc.device)
    loss = model(input_ids=tokens, labels=tokens).loss
    assert torch.isfinite(loss)
    acc.backward(loss)
    optimizer.step()
    optimizer.zero_grad()
    if acc.is_main_process:
        mlflow.log_metric("loss", loss.item(), step=step + (2 if args.resume else 0))
torch.cuda.synchronize()
elapsed = time.monotonic() - started
if acc.is_main_process:
    mlflow.log_metrics({
        "training_tokens_per_second": 2 * tokens.numel() * acc.num_processes / elapsed,
        "peak_gpu_memory_mib_rank0": torch.cuda.max_memory_allocated() / 2**20,
    })
acc.save_state(str(checkpoint))
if acc.is_main_process:
    with tempfile.TemporaryDirectory() as tmp:
        if acc.distributed_type != DistributedType.FSDP:
            module = acc.unwrap_model(model)
            module.eval()
            with torch.inference_mode():
                expected = module(input_ids=tokens).logits
                adapter_dir = Path(tmp) / "adapter"
                module.save_pretrained(adapter_dir)
                module.load_adapter(str(adapter_dir), adapter_name="roundtrip")
                module.set_adapter("roundtrip")
                torch.testing.assert_close(module(input_ids=tokens).logits, expected)
            mlflow.log_artifacts(str(adapter_dir), artifact_path="adapter")
        report = Path(tmp) / "result.txt"
        report.write_text(f"world_size={acc.num_processes} all_reduce={value.item()} resumed={args.resume}\n")
        mlflow.log_artifact(str(report))
    print(f"MLFLOW_RUN_ID={mlflow.active_run().info.run_id}", flush=True)
    mlflow.end_run()
acc.wait_for_everyone()
acc.end_training()
