# Pre-fit Gemma baseline

On the management host, render a review record from the signed Flamingo-capable deployment. Use the independent deployment fingerprint and worker node/provider identity from the approved GPU allocation. The renderer verifies the pinned managed API endpoint and CA, scoped operator identity, release, and Ready GPU node. Set `NEBIUS_SUBMIT_KUBECONFIG` to the private scoped operator config, `NEBIUS_STAGING_OPERATOR_IDENTITY` to its approved identity, and use the independently enrolled `NEBIUS_TRAINING_CLUSTER_ID`, `NEBIUS_CA_BUNDLE_SHA256` and `NEBIUS_API_ENDPOINT`. The only live workload-related query is a Node read.

```bash
python3 workloads/smoke/render-flamingo-baseline.py \
  --run "$baseline_run" --node "$node" --provider-id "$provider_id" \
  --expected-deployment "$deployment_sha256" --deadline-seconds "$deadline_seconds" \
  --gpu-limit-bytes "$gpu_limit_bytes" > baseline-review.json
```

The review record contains the exact proposed Job JSON and its SHA-256. One Job runs two sequential, fresh Gemma processes and the OpenTSLM freeze comparison. The Job publishes the two original receipt files and freeze.json together under `flamingo-baselines/<run>/baseline` on the training claim. Each process has a distinct receipt identity. Freeze reports their ordered digests and the measured repeat tolerance. Preserve the report, both receipts, Job and Pod UIDs, observed node, provider and image ID in the attended closeout.

## Pending release requirements

* Promote the paired trainer built with `FORECAST_BACKBONE=flamingo` from the committed OpenTSLM probe, plus its matching preflight image. Bind the signed release to the managed cluster and exact deployment commit.
* Add a separately reviewed, GPU-window-only admission path for these exact Job bytes or an equivalently constrained image entrypoint. The current `episode-submit-inputs` policy holds native submissions closed and its activated grammar only accepts `episode_job.py`. Bind the baseline code, arguments, resources, node placement, environment, writable output and read-only weights. Keep native fit, CPU-only session, and held-policy restrictions intact. Validate the effective Deny binding and scoped submitter with positive and negative server dry-runs.
* Extend the guarded launcher and off-host ledger with a baseline-specific reservation. Bind the Job digest, cluster, deployment, worker/VM, attempt, deadline and termination grace to the current approved GPU interval. Check available hour, dollar, cutoff and cleanup bounds. Reserve atomically before a create, reconcile uncertain creation by UID, and close out before another attempt. The paired-fit reservation requires a direct objective and does not fund this probe.
* Recheck target, deployment, enrolled storage, placement, cutoff and both shutdown schedulers at dispatch. Prepare the dedicated output directory for UID 1000, attest it empty and verify the staged base. Use the same released image and qualified target runtime for subsequent restore comparisons.

## Promote the approved pair

After successful freeze and fenced handoff, independently approve and record the two SHA-256 values **before fit**. In the existing attended storage context with the published baseline readable and the selected run state prepared, execute as the fit filesystem UID 1000. The helper publishes both verified files under `flamingo-baselines` inside the run state, preserving their exact bytes. An identical completed promotion can be checked again.

```bash
python3 workloads/smoke/promote-flamingo-baselines.py "$published_baseline" "$prepared_state" \
  --approved-baselines "$approved_first_sha256" "$approved_second_sha256"
```

This filesystem operation requires a separately authorized, writer-fenced storage handoff. The two approved digests also belong in the funded fit request. Selected-generation replay runs in the fit worker under that request.
