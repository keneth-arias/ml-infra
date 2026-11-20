# Implementation checklist

Each row is a proposed implementation slice. Complete the verification on the target environment before marking the row done.

| Done | Slice and likely paths | Depends on | Acceptance and verification |
|---|---|---|---|
| [x] | 1. Establish dev access and RBAC in `bootstrap/microk8s` | Existing node | Verify admin recovery access, enable RBAC and repeat the impersonated default-SA check for cluster-wide Secrets. It must return no. Confirm DNS and Calico stay healthy. |
| [x] | 2. Expose and qualify GPUs in `bootstrap/microk8s`, `workloads/smoke` | 1 | Node reports two allocatable GPUs. A pod requesting both executes CUDA on each device and completes a two-rank NCCL all-reduce with the expected sum. |
| [x] | 3. Bootstrap GitOps in `clusters/dev-bm`, `platform/overlays/dev` | 1 | Pin Flux, run preflight, bootstrap private repo access and SOPS. Reconcile a harmless namespace label, introduce drift and verify Flux restores it. |
| [x] | 4. Add render and schema checks in `ci` | 3 | CI renders configured clusters and rejects an invalid manifest and a floating production image reference. Verify server-side dry-run in dev after CRD installation. |
| [ ] | 5. Establish storage in `bootstrap/microk8s`, `platform/overlays/dev` | 3 | Verify local PVC persistence across pod replacement, object put/get and an off-host backup. Document capacity and restore paths. |
| [ ] | 6. Bring up MLflow in `platform/base`, `platform/overlays/dev` | 4, 5 | Log a run and artifact from a client pod. Restore PostgreSQL and objects into a fresh namespace and retrieve the same run and artifact checksum. |
| [ ] | 7. Publish a locked training image and PEFT fixture in `runtime`, `workloads/smoke` | 2, 6 | By digest, run a single-GPU BF16 LoRA update, log metrics and reload the saved adapter. Record driver, library versions, fixture and memory baseline. |
| [x] | 8. Qualify Accelerate DDP and FSDP in `workloads/accelerate` | 7 | Each profile uses both ranks, produces finite loss and records memory and tokens/sec. Resume FSDP from a complete checkpoint on a fresh pod and perform the next update. |
| [x] | 9. Submit from SkyPilot in `workloads/sky`, `platform/overlays/dev` | 8 | Scoped dev context discovers both GPUs. Launch the fixture by digest, find the MLflow run, cancel a second run and verify both GPUs return to the scheduler. |
| [ ] | 10a. Prepare provider identity and remote state in `bootstrap/nebius` | Nebius project and bootstrap access | Confirm the service account can read the target project using its authorized key. Initialize the S3 backend with bucket-scoped credentials and verify versioning and lockfile access. |
| [ ] | 10b. Review the MK8s Terraform plan in `bootstrap/nebius` | 10a | Pin the provider and training example revision. Verify formatting and validation pass. Review the saved plan for subnet, endpoint, CPU/GPU pool sizes, SKU availability, quota, estimated spend and replacements. |
| [ ] | 10c. Provision MK8s and bootstrap Flux in `clusters/prod-nebius`, `platform/overlays/prod` | 4, 10b and approved cloud provisioning | Apply the reviewed plan. Verify operator access, node readiness and GPU discovery. Check the prod overlay renders with independent credentials and storage, then confirm Flux reconciles the prod path. |
| [ ] | 11. Qualify the first cloud workload using `workloads/sky` | 9, 10c | Run the same digest on one GPU node. Verify metrics, adapter reload, checkpoint restore, fixed-input comparison and resource release. Gate any multi-node rollout on its own rendezvous, NCCL and recovery test. |

Local foundation checkpoint follows slices 1–5. Developer acceptance follows slice 9, when a SkyPilot submission exercises the full local path. Production acceptance follows the cloud qualification for the intended node count.

## Deployment record on bm

On 2026-11-19 the node advertised two RTX 5070 Ti GPUs with RBAC enforced. Flux reconciled the private repo with its read-only deploy key and decrypted the dev tracking credentials. PostgreSQL, VersityGW and MLflow became ready. A single-GPU BF16 LoRA run exported and reloaded an adapter. Two-rank NCCL, DDP and FSDP ran on both cards. A fresh pod resumed the FSDP checkpoint and logged its next two steps. SkyPilot submitted a two-GPU run and cancelled a separate test job. Images were imported directly into MicroK8s. The AirAware release and recovery work are tracked below.

The Nebius CLI and a Terraform data-source plan authenticated to the production project and read its default subnet in eu-south1. The project had zero MK8s clusters and the supplied Object Storage credentials listed zero buckets. The state bucket, production cluster sizing and cloud qualification remain open.


## AirAware Qwen handoff

- [x] Establish the application baseline and fresh-pod checkpoint recovery on the RTX 5070 Ti cluster. OpenTSLM #1–3, AirAware #89–90 and ml-infra #1 are merged into their respective main branches. [Recorded runs and input hashes](https://github.com/keneth-arias/airaware/blob/main/metadata/qwen-cluster-drill.json)
- [x] Publish the private AirAware runtime and run by digest on two GPUs. GHCR and the pod used `sha256:549f56bc614674399df79345941a44a7ebfa6182492ea61aa653c62711accea1`. [MLflow release check](http://bm.degu-fir.ts.net:5000/#/experiments/2/runs/9f6c78947ea646cd8b69f7fddc7d204e)
- [ ] Deferred by the owner. Restore the AirAware checkpoint and MLflow metadata/artifacts from independent storage when a destination is selected. The verified migration between local PVCs exercises same-host recovery.
- [ ] Select the production GPU configuration and spend limit for one qualification run. [Model evaluation scope](https://github.com/keneth-arias/airaware/issues/28)

## Lima forecast and episode pilot

- [ ] **Image and interchange.** Rebuild from pinned revisions and compare an in-container history-only export with OpenTSLM's synthetic reference. Record the pullable image digest, checkpoint identity and input/output hashes.
- [ ] **SkyPilot profiles.** Render one-GPU fit and inference jobs. Verify resources, read-only phase inputs, isolated run paths and retained state. Reject swapped role hashes, show fit pods cannot read calibration or held-out outcomes, and reject an inference archive containing targets before GPU initialization.
- [ ] **Recovery and telemetry.** Resume a short rank-4 run after interruption on a new pod. Retrieve its complete prediction artifact and comparison metrics from the recorded MLflow run. Qualify simultaneous fits only if measured host/GPU capacity permits.
- [ ] **Hourly pilot.** Using a frozen release, replay an entire station-day including a late reading, a missing window and a retried trigger. Reconcile expected, emitted and abstained forecasts; verify one canonical issuance per feed/station/origin across revisions, original issue times and a verified rollback from a release protected from training rotation.
- [ ] **External qualification.** After an approved costed Terraform plan, replay the same fixture on Nebius, verify numerical agreement and record actual cost and resource release. [Implementation order and dependencies](lima-episodes.md)
