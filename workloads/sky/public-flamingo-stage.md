# Stage the public Flamingo baseline on the retained training disk

Use this attended CPU procedure for the original enrolled Nebius allocation. The reviewed model is opentime/tslm-nov26 revision `5e030b001fb8d4be6d10ebb3a8b9bc3e6ef80983`. The signed preflight image supplies Python 3.11. An immutable ConfigMap supplies the reviewed stdlib worker and the nine version pins. The writer sees the public weights leaf and one fresh baseline output leaf. The reader, storage observer, submitter, and optional leaf owner have separate scopes.

## Prepare the review offline

On the management host use this reviewed checkout and the independently enrolled original deployment fingerprint, promotion receipt and all-bound receipt. The following commands produce the exact ConfigMap, Job and public object list for the owner. Keep the packet outside Git.

```bash
umask 077
sky=workloads/sky
run=public-gemma-20261122-01
state="$HOME/.local/state/ml-infra"
promotion="$state/flamingo-public-promotion-5e030b0/version-receipt.json"
binding="$state/current-four-disk-release/managed-all-bound.json"
scope="$state/current-four-disk-release/support-ops/gpu-package/scope.json"
deploy=1c92b48839f8fd1a660e641c85f26e1f9672344d2a20cf75e6fd4ca14839056f
render=(--promotion "$promotion" --binding "$binding" --expected-deployment "$deploy"
        --run "$run" --inventory-secret "stage-$run-inventory")
python3 "$sky/render-public-flamingo-stage.py" "${render[@]}" > "$packet"
sha256sum "$packet"
```

The packet names both original PVC/PV UIDs, the 128 GiB provider disk, the managed CPU node UID and provider ID, and the installed signed preflight image. Review the complete worker, source map and Pod specification. Pin the raw packet file SHA-256 independently. Runtime commands require separately issued owner approvals. Their SHA-256 arguments come from an independent channel. A local hash of a proposed approval does not confer authority.

The storage owner must hold exclusive control of the claim throughout preparation, execution and retirement. Confirm the original funding window, an enforced native submit hold, the original disk, both attachment layers, and fencing of every controller or external writer that could acquire the disk. Enforce the approved identities, names, images and exact admitted Pod mounts through the existing control plane. Keep the exclusive handoff through final receipt. RWO alone allows multiple Pods on one node. The worker also takes nonblocking locks on both leaf directories. Reconcile ambiguous creations by exact name and UID before considering another attempt.

### Conditional ownership request

Both leaves must exist or be approved for fresh directory creation by the kubelet. Check existing parent paths and authorize any necessary new path components explicitly. The retained Qwen digest and prior run state lie outside both mounts. When both leaves already satisfy the UID-1000 requirements, use the normal stager. If the owner establishes that fresh leaves need initialization, render the separately reviewable 60-second Job.

```bash
python3 "$sky/prepare-public-flamingo-leaves.py" render "${render[@]}" > "$owner_job"
sha256sum "$owner_job"
```

Obtain separate approval for that exact render, image, admission identity and CPU placement. The owner must verify that both targets are new and empty, the directory entries resolve to the intended leaves on the bound disk, the default service account has denied egress, and the existing workload hold remains active. The Job uses UID 0, a read-only root filesystem, no service account token, the single CHOWN capability, and two exact PVC subPath mounts. It checks both directories before changing the owner of a root-owned leaf to UID 1000. Group, file modes, descendants and siblings are outside the operation. A partial handoff can be reconciled using the same two empty leaves. Retire the approved Job and Pods with UID preconditions, recheck detachment and exclusively hand the claim to the stager. The staging submit command never starts the owner Job.

### Issue the version-specific inventory

The source custodian verifies effective object-store authority for exactly the nine listed object versions and supplies the dedicated reader credential. It must lack list, write, other version and other object privileges. Review the GET version grant and pin its private record. The source operator uses a separately approved kubeconfig scoped to create the named immutable Secret; admission must restrict creates to its exact name and shape. Protect that name against replacement throughout the handoff.

Set the enrolled `NEBIUS_TRAINING_CLUSTER_ID`, `NEBIUS_CA_BUNDLE_SHA256` and `NEBIUS_API_ENDPOINT`. For each command set `NEBIUS_SUBMIT_KUBECONFIG` to the private prod-nebius training context belonging to that command's independently authorized operator. Source approval includes the exact packet, effective grant, reader, identities and expiry. The credential file and grant stay on the source owner's host. The issuer creates the Secret once. Keep shell tracing and SDK debug logging off.

```bash
python3 "$sky/issue-public-flamingo-inventory.py" \
  --packet "$packet" --promotion "$promotion" --binding "$binding" --scope "$scope" \
  --approval "$source_approval" --approved-sha256 "$source_approval_sha256" \
  --reader-grant "$effective_grant" --reader-credentials "$private_reader" > "$issued"
```

The Secret contains only `inventory.json`. Its UID is an input to the storage handoff. Source credential material stays out of the Job, ConfigMap, approval packet and receipts. After expiry, a new attempt requires explicit reconciliation and a fresh inventory. Existing Secret names are never overwritten.

## Submit one approved CPU writer

Use the approved staging operator kubeconfig and the established storage observer environment (`NEBIUS_STORAGE_KUBECONFIG`, `NEBIUS_STORAGE_OPERATOR_IDENTITY`, and the ledger-independent read access needed by the Nebius disk handoff). Supply this owner-approved JSON structure in an owner-only file, replacing the example strings with independently verified values.

```json
{
  "purpose": "public-flamingo-stage",
  "exclusive_storage_handoff": true,
  "inventory_secret_immutable": true,
  "packet_file_sha256": "<approved exact packet file SHA-256>",
  "deployment_sha256": "<enrolled original deployment SHA-256>",
  "run": "public-gemma-20261122-01",
  "inventory_secret": "stage-public-gemma-20261122-01-inventory",
  "inventory_uid": "<actual issued UID>",
  "staging_operator": "<approved independent staging principal>",
  "storage_operator": "<approved storage observer principal>",
  "expires_at": "<owner expiry with UTC offset>"
}
```

```bash
bootstrap/nebius/with-credentials python3 "$sky/submit-public-flamingo-stage.py" \
  --packet "$packet" --approval "$stage_approval" --approved-sha256 "$stage_approval_sha256" \
  --promotion "$promotion" --binding "$binding" --scope "$scope" > "$enrollment"
```

The submitter creates the reviewed Job suspended, checks the admitted template and the exclusive handoff, then activates that exact Job with UID and resourceVersion preconditions. The staging role needs the corresponding exact-name patch authority. Reconcile uncertain calls using the emitted UIDs and current Job state.

The worker verifies projected program, inventory, version response headers, bounded lengths and SHA-256 while streaming. It reopens and hashes every staged file. It uses the weights directory rename and publishes the pinned `SHA256SUMS` last as the completion marker. An interrupted run can resume verified bytes inside the same public leaf after fencing the old writer. Reusing a completed snapshot validates every byte again. The output check verifies UID 1000, emptiness and write access without leaving a named entry.

## Collect and fence

Wait for terminal evidence before retirement. Use the storage observer identity and the approved packet's canonical JSON hash from enrollment. The capture reads the one successful child Pod and compares its source, actual placement and image with enrollment. Retain its raw file hash independently.

```bash
capture_args=(--promotion "$promotion" --binding "$binding" --expected-deployment "$deploy" --run "$run"
  --packet-sha256 "$(jq -er .packet_sha256 "$enrollment")"
  --job-uid "$(jq -er .job_uid "$enrollment")" --configmap-uid "$(jq -er .configmap_uid "$enrollment")"
  --inventory-uid "$(jq -er .inventory_uid "$enrollment")")
python3 "$sky/public-flamingo-stage-closeout.py" capture "${capture_args[@]}" > "$capture"
sha256sum "$capture"
```

The authorized owners retire the named Job (foreground propagation), its Pod, ConfigMap and inventory Secret using UID preconditions. Confirm each absence and keep alternate writers fenced. For any failed or partial run, collect failure evidence and reconcile the writer before reusing the leaf. Only the external fence command emits the staging receipt used by the baseline launcher.

```bash
bootstrap/nebius/with-credentials python3 "$sky/public-flamingo-stage-closeout.py" fence "${capture_args[@]}" \
  --capture "$capture" --capture-sha256 "$approved_capture_sha256" > "$staging_receipt"
```

Retain the enrollment, successful worker evidence, owner retirement records, and final receipt together. Subsequent GPU funding and baseline launch require their own authorizations.
