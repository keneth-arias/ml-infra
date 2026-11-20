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

MLflow permits tracking reads and writes from reachable LAN clients and permitted tailnet peers without a password. Tailnet traffic travels through Tailscale. SkyPilot requires HTTPS Basic authentication on both network routes. Use the shared operator login `skyadmin` only for people allowed to launch or cancel jobs. The owner can retrieve its password over SSH with `ssh kymai@bm.degu-fir.ts.net 'cat ~/.config/ml-infra/sky-lan-password'`. Cristopher can use the login `cristopher` on either route and retrieve its password with `ssh cristopher@bm.degu-fir.ts.net 'cat ~/.config/ml-infra/skypilot-password'`. These proxy logins share the same SkyPilot backend and job permissions. For direct LAN HTTPS, import the [public SkyPilot LAN CA certificate](bootstrap/microk8s/sky-lan-ca.crt) into the workstation trust store. The tailnet URL uses a publicly trusted certificate.

The `ml-access.service` user unit runs Caddy with the checked-in `bootstrap/microk8s/Caddyfile` installed in `~/.config/ml-infra/`. Its private environment file `sky-proxy.env` supplies the bcrypt hashes as `SKY_PASSWORD_HASH` and `SKY_CRISTOPHER_PASSWORD_HASH`. Keep the server's DHCP reservation at 192.168.50.172 for these URLs. Tailscale Serve forwards TCP port 5000 to loopback port 5000 and terminates HTTPS on port 46580 at the authenticated loopback proxy on port 46680. Tailnet grants control which peers can connect.

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

The SkyPilot task selects the AirAware runtime by its private GHCR manifest digest. It contains the OpenTSLM trainer and the AirAware TimeF reader. Weights, datasets and checkpoints are mounted at run time. On a new cluster, configure registry access for the training namespace with a package-read credential and use the image reference in `workloads/sky/airaware.yaml`. On bm, MicroK8s also caches this digest locally.

To develop a new image, check out the desired OpenTSLM and AirAware commits, install CPython 3.13.13 with uv and run:

```bash
bootstrap/microk8s/import-qwen.sh /path/to/OpenTSLM /path/to/airaware
```

The script makes a local build tagged with both source SHAs. Publishing and updating the digest in the SkyPilot task are separate release steps. The initial release uses OpenTSLM `7835d32fb478` and AirAware `6807cd6e7c47`.

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

### Versioned evaluation runs

The evaluation image uses OpenTSLM `5b1d45acbf01` and AirAware `24d22f759f08`. Stage the two full NPZ files and the committed AirAware `metadata/qwen-validation-inputs.json` as `/inputs/windows/lima-pm25-qwen-v1/{train.npz,validation.npz,manifest.json}` on the shared claim. The workload checks each array against the manifest and the manifest against the mounted TimeF release. The default `INPUT_VERSION=pilot` selects the original arrays. Each MLflow attempt records its input version and the versioned file hashes.

From the repository root, use the launcher to reserve one GPU for `single` and two for `ddp`, `fsdp` or `pair`. For a bounded comparison of the direct and persistence-correction heads, supply an unused logical run prefix and the same budget for both.

```bash
workloads/sky/launch-airaware.sh pair airaware-heads heads-01 -y \
  --env INPUT_VERSION=lima-pm25-qwen-v1 --env VALIDATE_AT=final \
  --env BATCH=4 --env STOP_STEP=60 --env SCHEDULE_STEPS=60
sky down -y airaware-heads
```

SkyPilot rejects concurrent pods mounting this ReadWriteOnce claim. The paired task mounts it once. Two independent trainers see one GPU each, read the same arrays and write separate MLflow runs and checkpoint directories with `-direct` and `-persistence` suffixes. Wait for both trainers before releasing the pod. Either trainer can cause the job to fail. Use the individual mode to resume just one unfinished head. The paired launcher requests 8 CPUs and 40 GB of host memory to accommodate simultaneous model loads and checkpoint writes.

The first local timing used 60 FP32 updates per head, global batch 4, seed 27 and the same 240 training windows in identical order. The interval from the first MLflow run start to the last finish was 45.657 s for the pair. The two successive DDP runs used 62.527 s in total across their MLflow intervals. Each interval includes model loading and the final checkpoint write. Cluster provisioning and input verification precede the interval. Peak sampled container memory during the pair was 31.69 GiB including file cache. These measurements qualify the scheduling path for the repeated performance comparison in AirAware issue #28.

To export forecasts in a SkyPilot job, set the package path explicitly in the task's `run` script. SkyPilot configures its own Python path for Ray workers.

```bash
export PYTHONPATH=/opt/forecast/src
/opt/conda/bin/python -m opentslm.forecast_inference \
  --checkpoint /checkpoints/heads-01-direct/step-00000060 --weights /inputs/qwen/weights \
  --data /inputs/windows/lima-pm25-qwen-v1/validation.npz \
  --output /checkpoints/heads-01-direct/validation-predictions.npz
```

For the full model comparison, set `AIRAWARE_MODEL_STATE=1` when calling `workloads/sky/launch-airaware.sh`. The launcher renders the pinned task with the retained `training/airaware-model-state` claim at `/checkpoints`. Its 64 GiB budget is dedicated to new model states. Frozen inputs still come from the read-only mount on `airaware-checkpoints`. Use the same setting to resume or restore these runs. The default profile continues to find existing qualification checkpoints at their original paths. Both claims use local storage on bm.

To use the model volume with a separate `sky exec` task, render the same profile with `uv run --script workloads/sky/model-state.py workloads/sky/airaware.yaml`. The claim binds on first use. At global batch 16 each full state occupies about 6.52 GiB. Keeping two completed generations plus one in progress per head needs about 39.1 GiB for two heads. Monitor actual usage and free bytes before starting other writers.

For the hybrid Qwen LoRA experiment, set `LORA=1` and choose `TRAIN_MODE=ddp`
or `TRAIN_MODE=single`. Use a new `RUN_ID` for the adapter run. Its checkpoint
holds the trainable adapters, projector, readout and head plus the optimizer
state. The pinned base snapshot is required at restore time. Resume with the
same image digest, model mode, seed, head, batch and schedule used at launch.

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

### Matched two-channel forecasts

The `joint` profile starts one direct LoRA fit per RTX 5070 Ti. Both fit the same targets.
The `first` process sees the first history channel and `both` sees both. It mounts
`airaware/joint-inputs` read-only at `/inputs` and the pinned base weights at
`/inputs/qwen`. Stage the historical TimeF release in `timef/`, its matrices and
verified scaler in `windows/lima-pm25-pm10-v1/`, and a top-level `SHA256SUMS`
covering the TimeF and input bundle manifests. Keep external releases outside these mounts.

On bm the profile uses the locally imported digest recorded in `model-state.py`.
`runtime/Dockerfile.joint` extends the existing evaluation image. Its `forecast` build
context is a Git archive of the trainer commit containing `src/opentslm` and
`scripts/train_qwen_forecast.py`; pass that same SHA as `FORECAST_SOURCE_REVISION`.
Import the built image into MicroK8s and verify the node's digest before launching.

```sh
workloads/sky/launch-airaware.sh joint lima-pm-pair lima-pm-joint-v1 -y -d \
  --env STOP_STEP=15745 --env SCHEDULE_STEPS=15745
```

Each process writes an attempt log under its own run directory in the model-state PVC.
After the final scheduled validation, it restores the selected checkpoint and writes
`step-XXXXXXXX-validation.npz` in original units. The output identifies its checkpoint,
input view, scaler, channels, forecast hours and window IDs.

### Lima candidate heads

Build the image on bm with `bootstrap/microk8s/build-episodes.sh "$forecast_checkout" "$airaware_checkout" IMAGE_TAG`. The qualified source revisions are OpenTSLM `9b5d61767b6b7db53a17980f111528ab3742fc77` and AirAware `82cf16edde8dbdeb7333256f6c1811be2db071dd`. The build takes committed revisions of ml-infra, OpenTSLM and AirAware. For uncapped GPU allocation, use a fresh pod and an episode image built from ml-infra commit `7d9818c` or a descendant. Images that launch training through `gpu_worker.py` retain their 16 GB allocator cap. Import the image to MicroK8s with the same digest referenced in the profile. For new paired point controls, use the OpenTSLM revision recorded in [local qualification](tasks/lima-qualification.md). The fit and inference roles each mount their own read-only bundle. Create a new run directory under `airaware/episodes/RUN` on the inputs claim and `episodes/RUN/{state,output}` on the model-state claim.

Use `workloads/sky/prepare-episode-smoke.py` with the synthetic AirAware fixture, the phase directory and the base weights manifest to generate each qualification bundle. The inference phase also takes the chosen checkpoint manifest. Each call prints the immutable bundle digest. Both entry points use the same command and one GPU.

```sh
image=docker.io/ml-infra/airaware-episodes@sha256:5ca73291e6064ae8483286e2857cdffa627a30d8badaf4b34f9616fa110d0d84
python3 workloads/sky/episode-profile.py sky fit quantile "$run" --image "$image" \
  --bundle-sha256 "$fit_sha" --step 2 --schedule 2 --batch 4 > "$run.yaml"
sky launch -y -c "$run" "$run.yaml"
sky down -y "$run"
python3 workloads/sky/episode-profile.py job infer quantile "$run" --image "$image" \
  --bundle-sha256 "$infer_sha" --step 2 --batch 2 | kubectl create -f -
```

For a recovery exercise, first request step 1 of the two-step schedule and then render a fresh fit with step 2 and `--resume`. Use separate run directories to compare against an uninterrupted fit. Local training pods request RAM for scheduling and can use available node memory. CUDA allocations can use the available VRAM on each assigned card. The worker logs attempts, digests and peak GPU allocation to MLflow. Synthetic fits record training loss. Brier and interval coverage are evaluated separately against a scored observation bundle. On a reused SkyPilot pod the cgroup memory peak includes earlier jobs on that pod.

To verify the mounted files before reserving a GPU, use `workloads/sky/launch-episode.sh` with the same arguments as the renderer. It runs a CPU-only Kubernetes preflight, then submits the requested Job or SkyPilot fit. Use `--attempt 2` for a fresh attempt, `--save-seconds` for checkpoint cadence and `--deadline-seconds` for the Kubernetes Job runtime ceiling. Inference writes one immutable prediction file per step. An interrupted tracker can recover a verified published result using the same run, input, image and step. Release a SkyPilot cluster with `sky down` after its job finishes.

The [local qualification](tasks/lima-qualification.md) records the reference output, interruption drill, GPU allocation and MLflow attempts. To preserve a selected generation outside training rotation, run `python3 runtime/episode_release.py release.json RELEASE_SHA256 STEP_DIRECTORY RETAINED_DIRECTORY`. The release manifest names its mode, immutable image reference, checkpoint and base manifest digests and the digests of `policy.json`, `transform.json` and `calibration.json`. Episode releases also include `thresholds.json`. The command verifies the complete copy and publishes it under its release digest.

### Evaluate a retained concentration release

AirAware's Campo de Marte test prepares a receipt for all scheduled origins, a history-only inference bundle and separate scoring observations. After the registered observation cutoff, run the staging command on bm with the published cohort and release digests. The two roots are the mounted roots of the `airaware-checkpoints` and `airaware-model-state` claims. Step 14838 and seed 27 belong to the frozen Lima comparison. Use the historical export batch size of 32 for the reserved concentration test.

```bash
uv run --script runtime/stage_holdout.py "$COHORT" "$INPUTS_ROOT" "$STATE_ROOT" "$RUN" \
  --cohort-sha256 "$COHORT_SHA" --retained-release "$RELEASE_SHA" --step 14838 > "$LAUNCH"
workloads/sky/launch-episode.sh job infer "$HEAD" "$RUN" --step 14838 --seed 27 --batch 32 \
  --image "$IMAGE" --bundle-sha256 "$BUNDLE_SHA" --retained-release "$RELEASE_SHA" --staged-launch "$LAUNCH"
```

Use the head, image and bundle digest in the staging result. A result of `empty` goes directly to AirAware's inconclusive report. The `ready` path verifies the retained release and stages the inference bundle. The CPU preflight and GPU worker mount just the selected checkpoint generation read-only. The scoring archive stays with AirAware. The run output directory and its independent MLflow record bind the inference result used for scoring. [Test protocol](https://github.com/keneth-arias/airaware/pull/128)

If an existing stage fails its digest check, preserve it for diagnosis. Stage the original signed-off inputs under a fresh run ID and use the new launch record for the retry.
