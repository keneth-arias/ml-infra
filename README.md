# ML training infrastructure

Flux reconciles the bare-metal development cluster from `clusters/dev-bm`. Platform services run in `ml-platform`, and GPU workloads run in `training`. The host owns its GPU driver. The MicroK8s NVIDIA add-on configures the container runtime and advertises the two RTX 5070 Ti cards. Flux uses a read-only Git deploy key.

## Run a local fine-tune

Run these commands from the repo on `bm`. The checked-in SkyPilot task runs an offline LoRA fixture with two GPUs, logs its metrics and adapter in MLflow, and stores training state on the checkpoints PVC. Choose a distinct SkyPilot cluster name for a new experiment.

```bash
export KUBECONFIG="$HOME/.config/ml-infra/kubeconfig"
sky launch -y -c my-lora workloads/sky/lora.yaml
sky down -y my-lora
```

The dedicated kubeconfig targets `dev-bm` and scopes submission to `training`. The task uses a pod without a Kubernetes API identity. Tear down the SkyPilot cluster after the run to release its GPU reservation. Use your usual MicroK8s admin context in a separate shell for platform operations.

On the server, MLflow is at `http://127.0.0.1:5000` and SkyPilot at `http://127.0.0.1:46580`. From a workstation, forward the ports through your usual SSH connection to this host. Both endpoints are loopback-only. PostgreSQL stores run metadata and VersityGW stores proxied artifacts. The three PVCs are local to this node. The local checkpoints and encrypted tracking credentials need off-host backups for disaster recovery.

## Operate and recover

- Check reconciliation with `flux get sources git` and `flux get kustomizations`. Check the GPU count with `kubectl get node bm -o json | jq '.status.allocatable["nvidia.com/gpu"]'`.
- The manifests under `workloads/smoke/` are one-shot Jobs, outside Flux reconciliation. Submit the FSDP example with `kubectl create -f workloads/smoke/fsdp.yaml`. To resume on a fresh pod, give the Job a new name and append `--resume` to the trainer arguments while retaining its checkpoint path. Use a fresh path for each independent training run.
- The dev images live in the node's MicroK8s containerd cache. After rebuilding a node, run `bootstrap/microk8s/import-images.sh`. Bump the local image tag whenever its contents change and use the updated tag in Jobs, SkyPilot and Flux manifests.
- To restore GitOps on a replacement node, enable the MicroK8s RBAC and hostpath-storage add-ons and enable NVIDIA with `--gpu-operator-driver=host --gpu-operator-version=v25.10.0`. Apply the Flux bootstrap at `clusters/dev-bm/flux-system` with a new read-only Git deploy key. Restore the SOPS age identity from secure backup into the `sops-age` Secret in namespace `flux-system`, key `age.agekey`. The runner's protected copy is in `~/.config/ml-infra/dev.agekey`. Restore the PostgreSQL, object store and checkpoint volumes before starting their consumers.
- Local user services `mlflow-ui.service` and `skypilot-local.service` bind the two loopback endpoints. Their unit files are in `bootstrap/microk8s/`.

## Nebius

The [Terraform setup](bootstrap/nebius/README.md) uses the registered service-account key for the cloud API and a distinct S3 key for state. The production GPU SKU, node counts, budget and state bucket must be selected before provisioning. See [the plan](tasks/plan.md) and [acceptance checklist](tasks/todo.md).
