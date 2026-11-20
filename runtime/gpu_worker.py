"""Aplica el presupuesto de VRAM en el proceso que ejecuta el modelo."""

import json
import os
import runpy
import sys
from pathlib import Path

GIB = 1024**3


def configure(cuda, rank):
    cuda.set_device(rank)
    free, total = cuda.mem_get_info(rank)
    budget = min(12 * GIB, free - 3 * GIB)
    if budget <= 0:
        raise RuntimeError("La memoria libre de la GPU es insuficiente para la reserva de 3 GiB")
    cuda.set_per_process_memory_fraction(budget / total, rank)
    print(json.dumps({"cuda_device": rank, "allocator_limit_bytes": budget}), flush=True)


def main():
    import torch

    configure(torch.cuda, int(os.environ.get("LOCAL_RANK", 0)))
    del sys.argv[0]
    if sys.argv[0] == "-m":
        del sys.argv[0]
        runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
    else:
        sys.path[0] = str(Path(sys.argv[0]).resolve().parent)
        runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
