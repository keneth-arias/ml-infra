# GPU training infrastructure plan

Build a private infra repo named `ml-infra` beside the workload repos. Use the existing MicroK8s server for development and target Nebius Managed Service for Kubernetes (MK8s) for production. MK8s lets SkyPilot target Kubernetes in both environments and lets Flux reconcile the same platform definitions. No cloud resources are required for the first local milestones.

## AirAware Qwen milestone

The [Lima forecast and episode pilot plan](lima-episodes.md) extends the completed point-forecast workload with interval estimates, calibrated episode probabilities and a private hourly replay. Its [acceptance checklist](todo.md#lima-forecast-and-episode-pilot) covers local qualification through an optional Nebius run.

The first application training baseline is complete. AirAware used a frozen TimeF release with 512 training windows and 128 chronological validation windows. The four-update pilot consumed 16 training windows. The single-GPU, replicated DDP and sharded FSDP2 runs used the same global batch. The interrupted FSDP2 run restored step 2 on a fresh pod and reproduced steps 3–4, including the inspected tensors and validation MAE. Hybrid LoRA also resumed. Eight checkpoint generations were verified on the retained 64 GiB claim. [OpenTSLM trainer](https://github.com/keneth-arias/OpenTSLM/pull/3) · [AirAware run evidence](https://github.com/keneth-arias/airaware/blob/main/metadata/qwen-cluster-drill.json) · [SkyPilot integration](https://github.com/keneth-arias/ml-infra/pull/1)

On the 128 validation windows, persistence achieved MAE 10.0107 µg/m³, direct Qwen 13.5589, and Qwen with a persistence correction 9.8225. Keep this as a short infrastructure and model pilot. AirAware issue #28 owns the budgeted chronological evaluation by station and horizon. The AirAware SkyPilot task now uses a private GHCR digest. A one-update, two-GPU LoRA release check on bm verified the frozen inputs and logged a complete checkpoint. The next infra decision is a costed Nebius qualification configuration. [Release run](http://bm.degu-fir.ts.net:5000/#/experiments/2/runs/9f6c78947ea646cd8b69f7fddc7d204e) The owner deferred independent-storage backup and restore. Take that same workload, data revision and evaluation configuration into the first costed Nebius plan. [AirAware evaluation](https://github.com/keneth-arias/airaware/issues/28)

## Starting point on bm

Read-only inventory on 2026-11-19 found one MicroK8s v1.35.6 node on the 1.35/stable snap channel, Calico, DNS, 32 logical CPUs, 59 GiB host RAM and about 901 GiB free on the mounted root filesystem. Linux sees two RTX 5070 Ti GPUs with 16,303 MiB each, compute capability 12.0 and NVIDIA driver 615.71.09. The GPUs communicate across a PCIe host bridge. Kubernetes currently exposes no allocatable GPUs or StorageClasses. Only the default system namespaces exist. The NVIDIA, storage and RBAC add-ons are disabled. An impersonation check allowed the default namespace's default service account to read Secrets cluster-wide. Resolve that before introducing registry, tracking or cloud credentials.

Use the installed local Kubernetes minor as the first validation target. Choose the Nebius minor from versions supported in the intended region and by the chosen Flux, SkyPilot and GPU stack. Record both minors and validate overlays against each. Available cloud capacity, production identity, object-storage region, quotas and network policy enforcement still need verification.

## Proposed repository

```text
bootstrap/
  microk8s/                # repeatable host prerequisites, add-on versions, recovery
  nebius/                  # Terraform for network, MK8s, node pools, IAM and storage
clusters/
  dev-bm/                  # flux-system and dev Flux Kustomizations
  prod-nebius/             # independent Flux bootstrap and production pins
platform/
  base/                    # namespaces, tenancy, MLflow and SkyPilot access
  overlays/{dev,prod}/     # storage, secret refs, RBAC bindings and resource budgets
runtime/                   # CUDA training image, dependency lock, compatibility record
workloads/
  smoke/                   # tiny redistributable model/data, train + restore checks
  accelerate/              # single GPU, two-rank DDP and FSDP configs
  sky/                     # common task and environment-specific resource profiles
ci/                        # render, schema and runtime checks
```

The infra repo owns a reference training workload. Application repos own model code, datasets' version manifests and experiment configs. Publish a workload image derived from the locked runtime and promote its digest. A run records the source SHA, image digest, model revision, dataset revision, Accelerate config, effective batch size, GPU type and checkpoint URI. A developer should be able to identify the exact inputs to a Nebius run from its MLflow record.

## Ownership and reconciliation

| Owner | Local development | Nebius production |
|---|---|---|
| Host or cloud bootstrap | Keep the host driver. Pin the MicroK8s NVIDIA add-on's GPU Operator version, use host-driver mode and let it configure snap containerd. Enable RBAC and provision a dedicated directory for local PVCs. | Terraform provisions the cluster, CPU and GPU pools, identity, private networking and buckets. Verify who supplies the node image, device plugin, CSI driver and GPU networking before installing any additional controller. Keep remote state encrypted and access controlled. |
| Flux | Bootstrap one read-only Git deploy key for the dev path. Reconcile namespaces, policies, service accounts, quotas and MLflow. | Use separate credentials and the prod path. Keep platform services on CPU nodes and reserve GPU nodes for training via taints, tolerations and affinity. Promote pinned charts and images by reviewed PR. |
| SkyPilot | Start with a pinned client/API server on the developer machine and a dedicated Kubernetes context scoped to the training namespace. Launch short-lived jobs. | Use a separately authenticated submitter with a restricted prod context. Set accelerator type, count, node count and budget explicitly. Permit production submission only through the approved identity. |
| Training process | Accelerate starts one PyTorch rank per allocated GPU. PEFT supplies adapters. | Use the same image and training entry point. Change placement, process topology and storage endpoints through profiles. |

Flux's dependency chain is namespaces and access → storage/secret prerequisites → stateful backends → MLflow → submission readiness. Express ordering with Flux Kustomization dependencies and health checks. Keep SkyPilot-created pods and individual experiments outside reconciled directories so a Git revert cannot replay training. Scope Flux reconciliation service accounts to their owned resources. Keep decryption keys out of Git, use separate SOPS recipients per environment, and protect namespace and data-volume deletion from routine pruning. [Flux tenancy](https://fluxcd.io/flux/installation/configuration/multitenancy/) · [MicroK8s GPU support](https://canonical.com/microk8s/docs/addon-gpu)

## Provisioning Nebius Managed Kubernetes with Terraform

Use `bootstrap/nebius/` as the Terraform root for production. Bootstrap a private, versioned state bucket independently of this root so that replacing the cluster preserves its state history. Configure the S3 backend with the chosen region's Nebius endpoint, a dedicated state key such as `prod/mk8s/terraform.tfstate` and `use_lockfile = true`. Confirm the workspace's `KEY_ID` and `SECRET_KEY` were issued for Object Storage and have the required bucket access before mapping them to `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the runner. Keep the state bucket separate from training artifacts. [Nebius state backend guide](https://docs.nebius.com/object-storage/store-terraform-state)

Give the provisioning service account project-scoped permissions for the planned resources. Look up its registered authorized-key ID and locate the private key matching `public.pem`. If the private key is unavailable, register a fresh pair. Supply the account ID, key ID and path to the private key through the runner environment. Keep the private key outside the checkout and restrict access to it and the local `.env` to the owner. Commit the following provider configuration alongside the tested provider constraint and `.terraform.lock.hcl`. [Provider authentication](https://registry.terraform.io/providers/nebius/nebius/latest/docs/guides/authentication)

```hcl
provider "nebius" {
  service_account = {
    account_id_env       = "NEBIUS_SA_ID"
    public_key_id_env    = "NEBIUS_AUTHORIZED_KEY_ID"
    private_key_file_env = "NEBIUS_PRIVATE_KEY_FILE"
  }
}
```

Adapt a pinned revision of the official training cluster example. Set the project, region, subnet, Kubernetes minor, API endpoint access, CPU pool and GPU pool explicitly. Select the smallest available GPU preset that fits the qualification run and check its quota and hourly cost. Record which GPU components the chosen node image supplies. [Nebius training cluster example](https://github.com/nebius/nebius-solution-library/tree/main/k8s-training)

Run `terraform fmt -check`, `terraform init`, `terraform validate` and `terraform plan -out=mk8s.tfplan`. Review the target project, network exposure, node counts, costs and any replacements before applying the saved plan. Keep saved plans in protected runner storage. After apply, use the operator's Nebius CLI profile with `nebius mk8s cluster get-credentials --id <cluster-id>`, verify the selected context and node readiness, then bootstrap Flux at `clusters/prod-nebius/`. Give SkyPilot its own namespace-scoped submission identity with the discovery permissions its backend requires. [Connect to MK8s](https://docs.nebius.com/kubernetes/connect)

## First local training workload

1. Prove a disposable Kubernetes pod can request both GPUs and run a real CUDA kernel on each. Build the runtime with a PyTorch/CUDA distribution supporting Blackwell sm_120. CUDA 12.8 wheels in PyTorch 2.7 are a documented starting point for Blackwell support. Select a currently supported release and pin the tested Python, torch, CUDA, Transformers, PEFT, Accelerate, MLflow client and optional bitsandbytes versions. Compile or select binaries for the production GPU architecture too. Test optional fused or quantized kernels independently. [PyTorch Blackwell support](https://pytorch.org/blog/pytorch-2-7/)
2. Start with BF16 LoRA on a small causal LM, a fixed dataset revision, a short sequence and microbatch of one. Verify finite loss, parameter updates confined to the intended adapter, adapter export and reload. Track peak allocated memory per rank and tokens/sec. Measure before increasing context or model size. Offer QLoRA after a 4-bit load, backward pass and adapter reload succeed on this host.
3. Run the same fixture on two GPUs in one pod. Validate NCCL all-reduce and two distinct device assignments first. DDP replicates the base model on each card. Use it as the throughput baseline when one replica fits. Keep a separate Accelerate FSDP profile for sharding, with a pinned FSDP generation, matching PEFT wrapping/frozen-parameter handling and checkpoint format. Exercise that profile on the small fixture before scaling model size. Budget each rank for activations and transient all-gathers within its own 16 GB. Measure PCIe communication before expecting a speedup. [Accelerate FSDP](https://huggingface.co/docs/accelerate/usage_guides/fsdp) · [PEFT FSDP](https://huggingface.co/docs/peft/accelerate/fsdp)
4. Let every required rank participate in checkpointing. Use global rank zero for MLflow metrics and publishing the completed checkpoint manifest. Capture optimizer, scheduler, RNG and training progress in resumable checkpoints. Keep the deployable PEFT adapter as a separate artifact. Interrupt a run, restart it on a fresh pod and verify it advances past the saved step. Record a GPU memory and throughput baseline for each profile.

## Data, tracking and job submission

- Run one private MLflow tracking service on CPU, backed by PostgreSQL for runs and an S3-compatible store for artifacts. For self-contained dev, use PostgreSQL and a pinned S3-compatible service on dedicated local PVCs. On Nebius, use a production PostgreSQL endpoint with backups and a separate Nebius Object Storage bucket. Expose MLflow locally through a port-forward for developers and ClusterIP for jobs. Configure MLflow artifact proxying so the tracking server holds artifact-store credentials. Use authenticated TLS access in production. [MLflow tracking server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/)
- Give training jobs their own narrowly scoped object-store identity for direct checkpoint writes and dataset reads. Store immutable datasets and completed checkpoints under versioned prefixes. Write a completion manifest after all shards upload. Keep disposable model/tokenizer caches on local disk. Cap cache and checkpoint growth, leave space for containerd and back up PostgreSQL and important objects off the server. Restore both to a fresh dev namespace before calling the tracking stack recoverable.
- For SkyPilot, select only the intended Kubernetes context, map its accelerator label to the GPU type actually discovered by the pinned SkyPilot version, and request two whole GPUs on one node for the local distributed profile. Confirm SkyPilot's image requirements against the training image. Specify CPU, memory, shared memory and scratch space, leaving capacity for MicroK8s and MLflow. Limit the dev namespace to two GPU requests. Use the narrowest Kubernetes backend RBAC supported by the chosen execution mode, including any required read-only node discovery. Test submission, logs, cancellation and resource release. [SkyPilot Kubernetes setup](https://docs.skypilot.ai/en/latest/reference/kubernetes/kubernetes-setup.html) · [Task YAML](https://docs.skypilot.ai/en/latest/reference/yaml-spec.html)

## Promotion and recovery

CI renders every Kustomize overlay and Helm release, validates schemas against each target minor and checks for floating image tags, plaintext credentials and production references to local paths. Run API-server dry-runs against an environment with the matching CRDs. A local smoke run must produce an MLflow record, a reloadable adapter and a resumable checkpoint. Reference their digests or version IDs in the promotion PR.

The first cloud run uses the same image digest, fixture, seed and effective batch size on a single Nebius GPU node. Compare loss within a tolerance derived from repeated baseline runs and compare the restored model on fixed inputs. Capture throughput and peak memory separately for the cloud GPU. For multi-node production, add a separate qualification run with one launcher per node, a shared rendezvous endpoint, unique machine ranks and matching world size. Validate cross-node NCCL transport on the selected fabric, object-store checkpoint access from every node, interruption/restart and GPU pool scale-down. Set a time or cost limit for qualification jobs. The single-server result qualifies code and artifacts for that test. [Nebius Managed Kubernetes](https://docs.nebius.com/kubernetes) · [Nebius Object Storage](https://docs.nebius.com/object-storage)

Recover a platform rollout by reverting its pinned Git revision and checking Flux readiness. Back up the tracking database before schema migrations and test a compatible restore if rollback crosses a migration. Recover a training run by selecting the last complete checkpoint and submitting a new run linked to its predecessor. Rehearse both paths in dev.
