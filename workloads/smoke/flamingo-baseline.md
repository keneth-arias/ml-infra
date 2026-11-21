# Pre-fit Gemma baseline

On the management host, render a review record from the signed Flamingo-capable deployment. Use the independent deployment fingerprint and worker node/provider identity from the approved GPU allocation. The renderer verifies the pinned managed API endpoint and CA, scoped operator identity, release, and Ready GPU node. Set `NEBIUS_SUBMIT_KUBECONFIG` to the private scoped operator config, `NEBIUS_STAGING_OPERATOR_IDENTITY` to its approved identity, and use the independently enrolled `NEBIUS_TRAINING_CLUSTER_ID`, `NEBIUS_CA_BUNDLE_SHA256` and `NEBIUS_API_ENDPOINT`. The only live workload-related query is a Node read.

```bash
python3 workloads/smoke/render-flamingo-baseline.py \
  --run "$baseline_run" --node "$node" --provider-id "$provider_id" \
  --expected-deployment "$deployment_sha256" --deadline-seconds "$deadline_seconds" \
  --gpu-limit-bytes "$gpu_limit_bytes" > baseline-review.json
```

The review record contains the exact proposed Job JSON and its SHA-256. The bounded Job first runs the packaged AWS parity comparator against the same pinned base. Budget the sequential reference and candidate loads, both language reference processes and the freeze comparison in its deadline. The comparator verifies prompt IDs, observation masks, latents, logits and generated IDs and publishes a distinct reference-parity.json receipt. Its SHA-256 is in freeze.json alongside the two independent language receipt hashes. The Job publishes the two original receipt files and freeze.json together under `flamingo-baselines/<run>/baseline` on the training claim. Each process has a distinct receipt identity. Freeze reports their ordered digests and the measured repeat tolerance. Preserve the report, both receipts, Job and Pod UIDs, observed node, provider and image ID in the attended closeout.

## Fund and submit the baseline

Use the signed deployment checkout, its pinned Flamingo candidate, the **original** GPU allocation scope installed on bm, the enrolled all-bound receipt and the Kubernetes UID of the approved worker. Keep all inputs in an owner-only directory. Use a fresh run name for a new authorization. Set `umask 077` before rendering the review. The staging operator verifies all weight manifest entries on the funded training disk and prepares the empty `flamingo-baselines/<run>` directory owned by UID 1000. After fencing the staging writer, preserve an owner-only JSON receipt containing the following fields and its supporting evidence.

```json
{
  "cluster_id": "<managed cluster ID>",
  "run": "<baseline run name>",
  "disk_id": "<enrolled training disk ID>",
  "weights_manifest_sha256": "7f8562992f1cf3852ced581f331247c6420fc90292d45453424c77f0a29c199f",
  "output_subpath": "flamingo-baselines/<baseline run name>",
  "output_uid": 1000,
  "output_empty": true,
  "writer_fenced": true
}
```

From the repo root on bm, generate the exact funding request. Use Python with the existing management host PyYAML and boto3 dependencies.

```bash
umask 077
tool=bootstrap/nebius/management/flamingo-baseline.py
inputs=(--scope "$original_gpu_scope" --binding "$all_bound_receipt" --review baseline-review.json
        --staging "$staging_receipt" --node-uid "$approved_node_uid"
        --expected-deployment "$deployment_sha256")
python3 "$tool" describe "${inputs[@]}" > baseline-approval.json
```

The funding owner settles the preceding attempt, retains the same open VM interval, refreshes the existing accounting and publishes the resulting approval ledger with `baseline_approval` equal to this JSON object and `active_reservation` null. Record the resulting immutable ledger VersionId and exact byte SHA-256 independently. Keep the same installed cutoff and scope on both shutdown hosts. The launcher checks bm's active timer and installed scope hash and the live fw13 CronJob UID, spec, image and last successful tick.

```bash
bootstrap/nebius/with-credentials python3 "$tool" submit "${inputs[@]}" \
  --ledger-sha256 "$approved_ledger_sha256" --ledger-version "$approved_ledger_version" \
  --field-manager "$policy_manager" --session "$new_private_session" > baseline-submitted.json
```

The launcher verifies the managed API and operator, signed release, Ready worker UID and provider ID, detached training disk and unconditional Deny hold. A versioned ledger compare-and-swap reserves the deadline and termination grace within the remaining hour, dollar, drain and cutoff limits. The operator enrolls the reviewed Job with `suspend: true` and the reservation revision label, and compares its admitted spec to the renderer. The baseline policy permits one transition on the issued Job UID with the entire pod template preserved. The temporary role grants get and patch on this single name. The launcher exercises positive and negative server dry-runs, rechecks funding and placement, issues a UID/resourceVersion-tested patch and closes the native session. The printed receipt identifies the resulting off-host ledger version, exact Job UID and funding version.

If interrupted, retain the session and active hold. Once `intent.json` exists, use the existing native close command with the same operator, target and session to revoke any credential and restore the held policy. If `release-started.json` exists and the exact UID is running or complete, run the following before removing the Job to reconcile the release and publish its receipt idempotently.

```bash
bootstrap/nebius/with-credentials python3 "$tool" record --session "$new_private_session" --scope "$original_gpu_scope"
```

For an uncertain reservation or enrollment, inspect the versioned ledger and audit the exact name, reservation label and UID. A suspended or missing Job requires attended reconciliation before any fresh approval. The funding owner retains the hold through terminal Job and Pod evidence, storage writer fencing, receipt validation and accounting closeout. Preserve the reference parity report, freeze report, both receipts and their ordered digests for selection.

## Promote the approved pair

After successful freeze and fenced handoff, independently approve and record the two SHA-256 values **before fit**. In the existing attended storage context with the published baseline readable and the selected run state prepared, execute as the fit filesystem UID 1000. The helper publishes both verified files under `flamingo-baselines` inside the run state, preserving their exact bytes. An identical completed promotion can be checked again.

```bash
python3 workloads/smoke/promote-flamingo-baselines.py "$published_baseline" "$prepared_state" \
  --approved-baselines "$approved_first_sha256" "$approved_second_sha256"
```

This filesystem operation requires a separately authorized, writer-fenced storage handoff. The two approved digests also belong in the funded fit request. Selected-generation replay runs in the fit worker under that request.
