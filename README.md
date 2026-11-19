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

## Workstation access

| Network | MLflow | SkyPilot dashboard |
|---|---|---|
| LAN (192.168.50.0/24) | http://192.168.50.172:5000/ | https://192.168.50.172:46580/dashboard/ |
| Tailnet | http://bm.degu-fir.ts.net:5000/ | https://bm.degu-fir.ts.net:46580/dashboard/ |

MLflow permits tracking reads and writes from reachable LAN clients and permitted tailnet peers without a password. Tailnet traffic travels through Tailscale. SkyPilot requires HTTPS Basic authentication on both network routes. Use the shared operator login `skyadmin` only for people allowed to launch or cancel jobs. The owner can retrieve its password over SSH with `ssh kymai@bm.degu-fir.ts.net 'cat ~/.config/ml-infra/sky-lan-password'`. For direct LAN HTTPS, import the [public SkyPilot LAN CA certificate](bootstrap/microk8s/sky-lan-ca.crt) into the workstation trust store. The tailnet URL uses a publicly trusted certificate.

The `ml-access.service` user unit runs Caddy with the checked-in `bootstrap/microk8s/Caddyfile` installed in `~/.config/ml-infra/`. Its private environment file `sky-proxy.env` supplies the bcrypt hash as `SKY_PASSWORD_HASH`. Keep the server's DHCP reservation at 192.168.50.172 for these URLs. Tailscale Serve forwards TCP port 5000 to loopback port 5000 and terminates HTTPS on port 46580 at the authenticated loopback proxy on port 46680. Tailnet grants control which peers can connect.

PostgreSQL stores run metadata and VersityGW stores proxied artifacts. The three PVCs are local to this node. The local checkpoints and encrypted tracking credentials need off-host backups for disaster recovery.

## Operate and recover

- Check reconciliation with `flux get sources git` and `flux get kustomizations`. Check the GPU count with `kubectl get node bm -o json | jq '.status.allocatable["nvidia.com/gpu"]'`.
- The manifests under `workloads/smoke/` are one-shot Jobs, outside Flux reconciliation. Submit the FSDP example with `kubectl create -f workloads/smoke/fsdp.yaml`. To resume on a fresh pod, give the Job a new name and append `--resume` to the trainer arguments while retaining its checkpoint path. Use a fresh path for each independent training run.
- The dev images live in the node's MicroK8s containerd cache. After rebuilding a node, run `bootstrap/microk8s/import-images.sh`. Bump the local image tag whenever its contents change and use the updated tag in Jobs, SkyPilot and Flux manifests.
- To restore GitOps on a replacement node, enable the MicroK8s RBAC and hostpath-storage add-ons and enable NVIDIA with `--gpu-operator-driver=host --gpu-operator-version=v25.10.0`. Apply the Flux bootstrap at `clusters/dev-bm/flux-system` with a new read-only Git deploy key. Restore the SOPS age identity from secure backup into the `sops-age` Secret in namespace `flux-system`, key `age.agekey`. The runner's protected copy is in `~/.config/ml-infra/dev.agekey`. Restore the PostgreSQL, object store and checkpoint volumes before starting their consumers.
- Local user services `mlflow-ui.service` and `skypilot-local.service` bind the two loopback endpoints. Their unit files are in `bootstrap/microk8s/`.

## Nebius

The [Terraform setup](bootstrap/nebius/README.md) uses the registered service-account key for the cloud API and a distinct S3 key for state. The production GPU SKU, node counts, budget and state bucket must be selected before provisioning. See [the plan](tasks/plan.md) and [acceptance checklist](tasks/todo.md).

## AirAware Qwen training

Use the pinned Qwen trainer revision referenced by the SkyPilot task. Install
CPython 3.13.13 with the host's uv for the TimeNet reader environment. Build and
import the image from `bm`, passing absolute paths to the two source checkouts.

```bash
bootstrap/microk8s/import-qwen.sh /path/to/OpenTSLM /path/to/airaware
```

The task mounts `airaware/inputs` and `airaware/runs` from the `training/airaware-checkpoints` claim. Stage the verified TimeF release under `airaware/inputs/timef`, the exact
Qwen snapshot under `airaware/inputs/qwen/weights`, and the training and validation
arrays as `train.npz` and `validation.npz`. Use AirAware's
`tools/timenet-smoke/export_qwen_windows.py` to generate the arrays. On the staging
filesystem, run `sha256sum weights/* >SHA256SUMS` in `qwen`, then run
`sha256sum train.npz validation.npz timef/manifest.json qwen/SHA256SUMS >SHA256SUMS`
in `inputs`. Create the sibling `runs` directory before launching. The pod
verifies the manifest and dataset using the pinned SDK before starting training.

```bash
export KUBECONFIG="$HOME/.config/ml-infra/kubeconfig"
sky launch -y -c airaware-part workloads/sky/airaware.yaml --env RUN_ID=recovery-01 --env STOP_STEP=2
sky down -y airaware-part
sky launch -y -c airaware-resume workloads/sky/airaware.yaml --env RUN_ID=recovery-01 --env RESUME=1
sky down -y airaware-resume
```

Use the same `RUN_ID` and `SCHEDULE_STEPS` when resuming. Use a fresh `RUN_ID`
for the continuous control or each independent experiment. `TRAIN_MODE=ddp`
runs the replicated comparison. For `TRAIN_MODE=single`, override the request
with `--gpus rtx5070ti:1`. To compare forecast heads, use another run ID and
`HEAD=persistence`. MLflow records each attempt, including checkpoint metadata.

For the hybrid Qwen LoRA experiment, set `LORA=1` and choose `TRAIN_MODE=ddp`
or `TRAIN_MODE=single`. Use a new `RUN_ID` for the adapter run. Its checkpoint
holds the trainable adapters, projector, readout and head plus the optimizer
state. The pinned base snapshot is required at restore time. Resume with the
same image tag, model mode, seed, head, batch and schedule used at launch.

The Flux overlay declares a 64 GiB hostpath claim with a Retain policy.
For a new volume, create the input and run directories before the first launch.
To bring forward the earlier drill from `training/checkpoints`, use the one-shot
migration job after stopping writers. It copies inputs and complete generations,
then checks every checkpoint file against its manifest. Keep the original
volume until a restore from the new claim has succeeded.

```bash
kubectl create -f workloads/smoke/airaware-migrate.yaml
kubectl logs -n training job/airaware-checkpoint-migration
```

Hostpath uses the node's shared filesystem. Monitor actual bytes on the node
and keep a verified copy outside the node for disaster recovery.
