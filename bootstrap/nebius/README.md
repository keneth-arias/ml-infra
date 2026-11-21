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

### Check the managed DNS selector

Use the production operator kubeconfig to compare the CPU policy with the CoreDNS Service and its pod template after reconciliation. This check also works before any DNS pods are Ready.

```bash
set -euo pipefail
k=(kubectl --kubeconfig "$HOME/.kube/nebius-prod" --context prod-nebius)
selector=$("${k[@]}" -n kube-system get service coredns -o jsonpath='{.spec.selector}')
"${k[@]}" -n kube-system get deployment coredns -o jsonpath='{.spec.template.metadata.labels}' |
  jq -e --argjson dns "$selector" '. as $labels | ($dns|length) > 0
    and all($dns | to_entries[]; $labels[.key] == .value)'
"${k[@]}" -n sky-cpu get networkpolicy dns-only -o json |
  jq -e --argjson dns "$selector" '.spec.egress | length == 1 and .[0].to == [{
    namespaceSelector: {matchLabels: {"kubernetes.io/metadata.name": "kube-system"}},
    podSelector: {matchLabels: $dns}
  }] and (.[0].ports | sort_by(.protocol)) == [{port:53,protocol:"TCP"},{port:53,protocol:"UDP"}]'
```

The policy permits UDP and TCP port 53. During CPU qualification, resolve `kubernetes.default.svc.cluster.local` from the actual pilot pod and verify a connection to the Kubernetes API remains denied. Capture the Ready CoreDNS endpoints and both results before starting training.

## Reconcile the production platform

The [production root](../../clusters/prod-nebius/kustomization.yaml) manages the existing `sky-cpu` LimitRange `cpu`, ResourceQuota `pilot` and NetworkPolicy `dns-only`. The [operator overlay](../../platform/overlays/prod) owns namespaces, admission, RBAC and retained storage. The pilot grants no training submit or drain role. Its admission policy also holds native training submissions closed until a separately reviewed release binds approved images and entry points to their roles. Check for pre-existing bindings to either account, revoke any submit or exec grant after verifying its owner, and confirm the effective denials before issuing credentials. Applying a bundle does not remove an older out-of-band binding. Run `workloads/smoke/verify-training-access.py` with the scoped native identity to check the CPU-phase hold. Keep training inventory and tracking credentials outside the pilot.

Install the seven CRDs from the checked-in Flux 2.10.0 component manifest, then wait for `Established`. Stage the Restricted `flux-system` namespace, the read-only repository key `nebius-git-read` with pinned GitHub host keys, and the separate SOPS identity `nebius-sops-age`. The [production controller renderer](management/flux/render-controllers.py) selects two digest-pinned controllers from that exact manifest. They watch `flux-system` and run on the CPU pool. The kustomize controller impersonates `flux-system/nebius-platform`. Its [target Role](management/flux/access.yaml) permits get and patch on the three named objects. Bootstrap each object first. The controller installer runs separately from the Git source.

Before handoff, save the intended specs of those three existing objects from the verified bootstrap revision and record their UIDs. Review the bootstrap manager’s server-side diff. Verify the signed three-role image release, package pull credentials, scoped backend admission and DNS selector. Review ownership conflicts individually. The operator overlay also contains two 64 GiB delayed-binding claims with a Retain storage class. Stage them only in their funded window. They and their namespaces stay under operator ownership.

From a trusted clone on the target-pinned operator host, verify that the desired commit is merged. Stage both source objects suspended. After the CPU worker is Ready, apply the reviewed controller render, wait for both Deployments to become Available, then enable fetching alone. The source name includes the complete commit, so a previous fetch cannot serve as the new revision.

```bash
set -euo pipefail
git fetch origin main
git merge-base --is-ancestor "$DEPLOYMENT_COMMIT" origin/main
k=(kubectl --kubeconfig "$HOME/.kube/nebius-prod" --context prod-nebius)
set -a
. /etc/sky-cpu/target.env
set +a
python3 workloads/sky/private_api.py "${k[@]}"
render=bootstrap/nebius/management/render-flux-source.py
python3 "$render" "$DEPLOYMENT_COMMIT" | "${k[@]}" apply --server-side --field-manager=nebius-flux-bootstrap -f -
# In the funded CPU window, after reviewing the controller dry run and the bootstrap/Flux handoff:
uv run bootstrap/nebius/management/flux/render-controllers.py | "${k[@]}" apply --server-side --field-manager=nebius-flux-bootstrap -f -
for controller in source-controller kustomize-controller; do
  "${k[@]}" rollout status -n flux-system "deployment/$controller" --timeout=3m
done
python3 "$render" --fetch "$DEPLOYMENT_COMMIT" | "${k[@]}" apply --server-side --field-manager=nebius-flux-bootstrap -f -
src=gitrepository.source.toolkit.fluxcd.io/platform-$DEPLOYMENT_COMMIT
ks=kustomization.kustomize.toolkit.fluxcd.io/nebius-platform
"${k[@]}" -n flux-system wait --for=condition=Ready "$src" --timeout=3m
revision=$("${k[@]}" -n flux-system get "$src" -o json |
  jq -er --arg sha "$DEPLOYMENT_COMMIT" 'select(.spec.ref.commit == $sha and .status.observedGeneration == .metadata.generation and ((.status.artifact.revision | split("@") | last) == ("sha1:" + $sha))) | .status.artifact.revision')
rv=$("${k[@]}" -n flux-system get "$ks" -o json |
  jq -er --arg src "platform-$DEPLOYMENT_COMMIT" 'select(.spec.suspend and .spec.sourceRef.name == $src) | .metadata.resourceVersion')
python3 "$render" --fetch "$DEPLOYMENT_COMMIT" |
  jq --arg rv "$rv" '.items[1] | .metadata.resourceVersion=$rv | .spec.suspend=false' |
  "${k[@]}" apply --server-side --field-manager=nebius-flux-bootstrap -f -
"${k[@]}" -n flux-system wait --for=jsonpath='{.status.lastAppliedRevision}'="$revision" "$ks" --timeout=3m
```

Wait for the Kustomization’s current generation to be Ready and `status.lastAppliedRevision` to equal the verified source artifact revision. Repeat the DNS policy readback and scoped backend admission probe. Verify CoreDNS Ready endpoints and in-pod behavior separately.

**First-deployment recovery.** Apply the same suspended Kustomization with the same server-side field manager and verify the controller has finished any in-flight reconciliation. Suspend its Git source. Using the bootstrap manager, review the server-side diff of the three saved specs against their recorded names and UIDs, restore them and verify effective quotas and DNS egress. To restore a changed spec, test the saved UID and current resourceVersion in a JSON Patch before replacing that object’s spec. Review any additional labels or annotations owned by the release separately. Any ownership transfer must cover only the reviewed field changes. Both Flux objects can stay suspended for diagnosis. The Kustomization uses `prune: false` and `deletionPolicy: Orphan`. Earlier foundation commits can lack a production root. For a subsequent rollback, stage the previous verified root commit, repeat the source-revision check, then resume reconciliation.

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
