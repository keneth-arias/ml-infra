# Implementation checklist

Each row is a proposed implementation slice. Complete the verification on the target environment before marking the row done.

| Done | Slice and likely paths | Depends on | Acceptance and verification |
|---|---|---|---|
| [ ] | 1. Establish dev access and RBAC in `bootstrap/microk8s` | Existing node | Verify admin recovery access, enable RBAC and repeat the impersonated default-SA check for cluster-wide Secrets. It must return no. Confirm DNS and Calico stay healthy. |
| [ ] | 2. Expose and qualify GPUs in `bootstrap/microk8s`, `workloads/smoke` | 1 | Node reports two allocatable GPUs. A pod requesting both executes CUDA on each device and completes a two-rank NCCL all-reduce with the expected sum. |
| [ ] | 3. Bootstrap GitOps in `clusters/dev-bm`, `platform/overlays/dev` | 1 | Pin Flux, run preflight, bootstrap private repo access and SOPS. Reconcile a harmless namespace label, introduce drift and verify Flux restores it. |
| [ ] | 4. Add render and schema checks in `ci` | 3 | CI renders configured clusters and rejects an invalid manifest and a floating production image reference. Verify server-side dry-run in dev after CRD installation. |
| [ ] | 5. Establish storage in `bootstrap/microk8s`, `platform/overlays/dev` | 3 | Verify local PVC persistence across pod replacement, object put/get and an off-host backup. Document capacity and restore paths. |
| [ ] | 6. Bring up MLflow in `platform/base`, `platform/overlays/dev` | 4, 5 | Log a run and artifact from a client pod. Restore PostgreSQL and objects into a fresh namespace and retrieve the same run and artifact checksum. |
| [ ] | 7. Publish a locked training image and PEFT fixture in `runtime`, `workloads/smoke` | 2, 6 | By digest, run a single-GPU BF16 LoRA update, log metrics and reload the saved adapter. Record driver, library versions, fixture and memory baseline. |
| [ ] | 8. Qualify Accelerate DDP and FSDP in `workloads/accelerate` | 7 | Each profile uses both ranks, produces finite loss and records memory and tokens/sec. Resume FSDP from a complete checkpoint on a fresh pod and perform the next update. |
| [ ] | 9. Submit from SkyPilot in `workloads/sky`, `platform/overlays/dev` | 8 | Scoped dev context discovers both GPUs. Launch the fixture by digest, find the MLflow run, cancel a second run and verify both GPUs return to the scheduler. |
| [ ] | 10a. Prepare provider identity and remote state in `bootstrap/nebius` | Nebius project and bootstrap access | Confirm the service account can read the target project using its authorized key. Initialize the S3 backend with bucket-scoped credentials and verify versioning and lockfile access. |
| [ ] | 10b. Review the MK8s Terraform plan in `bootstrap/nebius` | 10a | Pin the provider and training example revision. Verify formatting and validation pass. Review the saved plan for subnet, endpoint, CPU/GPU pool sizes, SKU availability, quota, estimated spend and replacements. |
| [ ] | 10c. Provision MK8s and bootstrap Flux in `clusters/prod-nebius`, `platform/overlays/prod` | 4, 10b and approved cloud provisioning | Apply the reviewed plan. Verify operator access, node readiness and GPU discovery. Check the prod overlay renders with independent credentials and storage, then confirm Flux reconciles the prod path. |
| [ ] | 11. Qualify the first cloud workload using `workloads/sky` | 9, 10c | Run the same digest on one GPU node. Verify metrics, adapter reload, checkpoint restore, fixed-input comparison and resource release. Gate any multi-node rollout on its own rendezvous, NCCL and recovery test. |

Local foundation checkpoint follows slices 1–5. Developer acceptance follows slice 9, when a SkyPilot submission exercises the full local path. Production acceptance follows the cloud qualification for the intended node count.
