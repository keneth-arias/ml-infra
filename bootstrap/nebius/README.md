# Nebius managed training cluster

This root manages a worker-free MK8s foundation and optionally a private, fixed-size CPU or spot GPU group. Use a versioned, locked S3 backend per cluster. The CPU pilot uses one `cpu-d3 / 4vcpu-16gb` worker with a 128 GiB boot SSD. The fit uses one CPU support worker and one `gpu-rtx6000-a / 1gpu-24vcpu-218gb` worker with a $1.50/hour maximum spot bid. The cumulative experiment limits are $250, $100 in non-GPU costs and 100 allocated GPU-hours.

A new managed cluster starts with a private API. For public access, set the approved operator egress IPv4 /32 in private tfvars. Existing-cluster mode reads the supplied cluster and capped spot policy from their IDs. The CPU pilot and paired GPU experiment use the owned foundation. Review the state lineage and existing groups before changing any deployed configuration. Previously provisioned pools must be reconciled before adopting timed allocation windows.

## CPU admission before worker allocation

The standalone [CPU overlay](../../platform/overlays/prod/sky-cpu) targets Kubernetes 1.36. It supplies the Restricted `sky-cpu` namespace, workload and backend identities, quotas, image and Service admission policies, and DNS egress. The local development cluster keeps its own Flux configuration.

Before reconciling this overlay in production, verify the signed release manifest for the rootless CPU worker, trainer and preflight against the independently approved signer. Match the rootless role and index to `skyImage` in the rendered policy. The policy annotation pins the commit containing its qualification receipt. CI checks that receipt against the rendered image literal and verifies its registry signature, including when a newer candidate is staged. Use the bootstrap identity to confirm the package-scoped, pull-only `sky-cpu-pull` exists in `sky-cpu`. Keep its credentials outside Git. The hosted image check verifies registry bytes with the CI identity. The first authorized worker must also cold-pull with its namespace credential.

The existing objects are owned by the `nebius-pilot-bootstrap` field manager. Compare all rendered objects with its server-side dry run before handing them to Flux. Resolve field ownership during the reviewed handoff. After reconciliation and before allocating workers, use the scoped backend kubeconfig and the operator-pinned target environment. Pass the full rootless reference from the verified release.

```sh
uv run workloads/smoke/check-cpu-admission.py "$ROOTLESS_CPU_IMAGE"
```

The probe verifies the endpoint, CA, namespace and backend identity. It requires an image-policy denial for a changed digest and admission for the approved digest, both by server-side dry run. Repeat after policy bindings have propagated. Keep allocation closed on a mismatch. After a previous successful release, restore its reviewed policy and matching image reference to roll back, then rerun the probe. On a first deployment, keep submissions closed until the probe passes.

## Reconcile the production platform

The [production root](../../clusters/prod-nebius/kustomization.yaml) includes CPU admission, the native training and scoring boundaries, and two 64 GiB RWO claims. Their storage class waits for the first consumer and retains its disks. Namespace, storage class and claim annotations protect them from Flux pruning. Deleting the Flux Kustomization leaves its managed objects in place. Price retained disk capacity until explicit cleanup.

Install the seven source and kustomize CRDs from the reviewed Flux 2.10.0 component manifest and wait for `Established`. Use the separate `flux-system/nebius-git-read` SSH deploy key with pinned GitHub host keys and `flux-system/nebius-sops-age` decryption identity. Once the first worker is Ready, install the source and kustomize controllers on the CPU pool using reviewed image digests.

From a trusted clone, verify the intended deployment commit was merged, then stage the reconciliation objects with the production operator kubeconfig. The renderer checks that the commit contains the production root.

```bash
set -euo pipefail
git fetch origin main
git merge-base --is-ancestor "$DEPLOYMENT_COMMIT" origin/main
k=(kubectl --kubeconfig "$HOME/.kube/nebius-prod" --context prod-nebius)
python3 bootstrap/nebius/management/render-flux-source.py --suspend "$DEPLOYMENT_COMMIT" |
  "${k[@]}" apply --server-side --dry-run=server --field-manager=nebius-flux-bootstrap -f -
```

Use the same suspended render to stage the objects. Before activation, verify the signed image release, approved image-to-role mapping, DNS policy, pull credentials and the deployment diff against the target. Check retained namespaces and claims explicitly. After the worker is Ready, remove `--suspend` and apply the same commit. Confirm both Flux resources become Ready and `status.lastAppliedRevision` matches the source artifact revision and intended SHA. Verify workload health separately because the initial storage claims wait for a consumer. For rollback, render the prior verified deployment and review its prune diff before changing the Git pin.

## Credentials and plan

On this server, load the verified non-secret IDs with `. ~/.config/ml-infra/nebius.env`. The existing CLI profile is `ml-prod`. Run from this directory. Set `NEBIUS_SA_ID` and `NEBIUS_AUTHORIZED_KEY_ID` to the IDs supplied by IAM. The command wrapper points the provider at the protected private key in `~/.config/ml-infra/nebius-private.pem` and maps the workspace's owner-only S3 credential file into the subprocess environment. Override the paths with `NEBIUS_PRIVATE_KEY_FILE` or `ML_INFRA_ENV_FILE`. Keep these files on the runner.

1. Copy `terraform.tfvars.example` to `terraform.tfvars` and enter the project, subnet and Kubernetes version. Keep the foundation's `node_pools` empty. Confirm available GPU quota and price. Copy `backend.tfbackend.example` to `prod.tfbackend` and enter the private, versioned state bucket and regional endpoint.
2. Configure a named CLI profile using the same registered key. The final command below queries the cluster list.

   ```bash
   ./with-credentials sh -ec 'nebius profile create ml-prod --endpoint api.nebius.cloud --skip-auth --service-account-id "${NEBIUS_SA_ID:?}" --public-key-id "${NEBIUS_AUTHORIZED_KEY_ID:?}" --private-key-file-path "$NEBIUS_PRIVATE_KEY_FILE" --parent-id "${NEBIUS_PROJECT_ID:?}"'
   nebius --profile ml-prod mk8s cluster list
   ```

   Set `NEBIUS_PROJECT_ID` to the same project as `terraform.tfvars` before creating the profile.

3. Initialize the backend, check the configuration and produce a plan for review.

   ```bash
   umask 077
   ./with-credentials terraform init -reconfigure -backend-config=prod.tfbackend
   terraform validate
   ./with-credentials terraform plan -out=mk8s.tfplan
   terraform show mk8s.tfplan
   ```

4. For an approved worker-free foundation run `./with-credentials ./apply-foundation --apply mk8s.tfplan`. Use the [guarded worker commands](management/README.md) for every subsequent allocation. Both cleanup hosts need live project and state access before worker provisioning. For the private API, from a runner with subnet reachability, retrieve credentials into a dedicated kubeconfig before bootstrapping Flux.

   ```bash
   cluster_id=$(./with-credentials terraform output -raw cluster_id)
   nebius --profile ml-prod mk8s cluster get-credentials --id "$cluster_id" --internal --context-name prod-nebius --kubeconfig "$HOME/.kube/nebius-prod"
   kubectl --kubeconfig "$HOME/.kube/nebius-prod" get nodes
   ```

For offline verification, use `terraform init -backend=false`, `terraform fmt -check` and `terraform validate`. The resource layout follows the [Nebius training example](https://github.com/nebius/nebius-solution-library/tree/194faa5249939d7345f6db7f5bf60ccd2f3a6b2c/k8s-training).

### Stage fit and inner-validation data on the CPU

For the direct PM2.5 comparison, take the two role manifest digests from the approved AirAware cohort receipt. Render the staging Job with the immutable preflight image, fit digest, inner-validation digest, base weights digest and the name of a training-only inventory Secret.

```bash
python3 workloads/sky/stage-episode.py --phase fit --head direct \
  --image "$preflight_image" --weights-sha256 "$weights_sha256" \
  --bundle-sha256 "$fit_sha256" --validation-sha256 "$validation_sha256" \
  --source-prefix "$approved_https_prefix" --inventory-secret "$training_inventory" > "$stage_job"
```

Under that prefix the paired inventory’s `base`, `bundle` and `inner_validation` lists refer to exact versioned keys in `base/`, `trainer/fit/` and `trainer/inner_validation/`. Each list item supplies its relative `name`, byte `size`, `version_id` and a matching version-specific HTTPS `url`. Keep scorer objects and credentials in separately authorized storage. The stager checks the role manifest before fetching its data. It verifies both roles share the protocol, historical source, base and fit-derived threshold table before returning success. Collect the staging receipt from the Job logs within 24 hours of completion, before Kubernetes removes the Job. The receipt records cold copies or verified cache hits for each digest. A retry can reuse individually verified entries after a partial failure.

For a local qualification, supply `--base`, `--bundle` and `--validation-bundle` to `runtime/stage_episode_inputs.py` along with the digest and cache arguments. The managed Job requires the prepared training claim and the immutable digest of the slim `input-staging` target or a preflight image derived from it. Its command runs `/usr/local/bin/python /opt/ml-infra/stage_episode_inputs.py`.

After an interrupted copy, retry the same digest. Under its exclusive lock, the stager reclaims that digest’s partial directory before checking available disk space. Leave lock files in place. To reclaim an abandoned revision or an older randomly named `.staging-*` directory, first stop all writers on the claim and verify the directory is an unpublished partial. If verification fails on a published digest, stop its consumers and writers, quarantine that exact directory outside the cache, and restage from the approved source.

The episode Dockerfile packages the staging entry point and its dependencies. To check the committed CPU stage independently on bm, build the `input-staging` target from the archived runtime sources and invoke its CLI:

```bash
sha=$(git rev-parse HEAD)
git archive "$sha:runtime" Dockerfile.episodes episode_job.py stage_episode_inputs.py versioned_episode_inputs.py | \
  docker build --target input-staging --build-arg "INFRA_SOURCE_REVISION=$sha" -f Dockerfile.episodes -t "episode-stage:$sha" -
docker run --rm --network none "episode-stage:$sha" python /opt/ml-infra/stage_episode_inputs.py --help
```

This target qualifies input staging. The production three-image release still needs its complete CPU preflight and a trainer that verifies and consumes both role manifests.


### Run a pinned direct comparison

Build the trainer and slim CPU preflight with the paired OpenTSLM revision and the same infra sources. Supply both immutable image references and the three staged digests to `workloads/sky/launch-episode.sh --nebius job fit direct RUN_ID`. Select `--direct-objective normalized-mse` or `--direct-objective physical-mae`. Supply `--bundle-sha256` for fit, `--validation-sha256` for inner validation, `--cached-weights-sha256`, and both `--step` and `--schedule` set to the full epoch ceiling. Use distinct run IDs for the two arms and reuse the same ID, seed, image and digests when resuming an interrupted arm. The CPU preflight mounts the two cohorts and base read-only, verifies their identities and rejects a swapped role before GPU submission. Set `--gpu-limit-bytes` only to apply a measured cloud allocator budget.

The selected checkpoint and actual stopping step are recorded separately in `direct-result.json`. A retry uses `--resume --attempt N` and reuses the logical MLflow run. From the management host, set the authenticated tracking URI and run `uv run workloads/sky/direct-learning-curves.py --run CONTROL_MLFLOW_ID --run CANDIDATE_MLFLOW_ID --output NEW_DIRECTORY`. Supply both arms for each seed. The report checks the terminal receipt and checkpoint manifest before exporting the evaluation CSV and PNG. The cutoff and budget gate apply before launching either arm.

For a fit launched with `--graceful-stop`, use the verified training context, namespace, pod name and worker container from that attempt. Read back its UID before requesting a stop. Supply the run ID, scheduled step and attempt used at launch. The helper checks the pod UID again, writes the request inside that pod and verifies the generation named in its acknowledgement.

```bash
pod_uid=$(kubectl --context prod-nebius -n training get pod "$pod" -o jsonpath='{.metadata.uid}')
kubectl --context prod-nebius -n training exec "$pod" -c "$container" -- \
  env PYTHONPATH=/opt/forecast/src /opt/conda/bin/python /opt/ml-infra/episode_stop.py \
  --pod-uid "$pod_uid" --run-id "$run_id" --step "$step" --attempt "$attempt" --timeout 90
```

An interrupted fit exits 75. A stop before the first update can have no generation to restore. If the helper times out, check the worker exit and verify the last complete checkpoint before retrying within the original cutoff. Inspect quarantined `.damaged-*` and `.superseded-*` directories with writers stopped. Reclaim space only after verifying an independent restorable copy.
