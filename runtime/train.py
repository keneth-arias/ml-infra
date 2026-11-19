"""Offline dual-rank LoRA fixture. Run with accelerate launch."""
import argparse
from pathlib import Path

import mlflow
import torch
import torch.distributed as dist
from accelerate import Accelerator
from accelerate.utils import set_seed
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
for step in range(2):
    tokens = torch.randint(0, 256, (2, 32), device=acc.device)
    loss = model(input_ids=tokens, labels=tokens).loss
    assert torch.isfinite(loss)
    acc.backward(loss)
    optimizer.step()
    optimizer.zero_grad()
    if acc.is_main_process:
        mlflow.log_metric("loss", loss.item(), step=step + (2 if args.resume else 0))
acc.save_state(str(checkpoint))
if acc.is_main_process:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "result.txt"
        report.write_text(f"world_size={acc.num_processes} all_reduce={value.item()} resumed={args.resume}\n")
        mlflow.log_artifact(str(report))
    print(f"MLFLOW_RUN_ID={mlflow.active_run().info.run_id}", flush=True)
    mlflow.end_run()
acc.wait_for_everyone()
