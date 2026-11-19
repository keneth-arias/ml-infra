FROM pytorch/pytorch:2.9.1-cuda12.8-cudnn9-runtime@sha256:7b324d212a4450795b49edba9949b7cdc72429148a64e974334bfe5774d51385
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-server rsync sudo curl git ca-certificates netcat-openbsd lsof && rm -rf /var/lib/apt/lists/*
RUN /opt/conda/bin/pip install --no-cache-dir "transformers==4.57.6" "peft==0.18.1" "accelerate==1.12.0" "mlflow-skinny==3.18.0" && /opt/conda/bin/python -c "import torch,peft,accelerate,mlflow; from transformers import LlamaForCausalLM"
COPY train.py /opt/ml-infra/train.py
