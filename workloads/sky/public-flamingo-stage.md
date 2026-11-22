# Stage the public Flamingo baseline on the retained training disk

Use this attended CPU procedure for the original enrolled Nebius allocation. The reviewed model is opentime/tslm-nov26 revision `5e030b001fb8d4be6d10ebb3a8b9bc3e6ef80983`. The signed preflight image supplies Python 3.11. An immutable ConfigMap supplies the reviewed stdlib worker and the nine version pins. The writer sees the public weights leaf and one fresh baseline output leaf. The reader, storage observer, submitter, and optional leaf owner have separate scopes.

## Prepare the review offline

On the management host use this reviewed checkout and the independently enrolled original deployment fingerprint, promotion receipt and all-bound receipt. The following commands produce the exact ConfigMap, Job and public object list for the owner. Keep the packet outside Git.

```bash
umask 077
sky=workloads/sky
review_dir=$(mktemp -d)
packet="$review_dir/stage-review.json"
owner_job="$review_dir/conditional-owner-job.json"
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

### Claim admission and exclusive storage handoff

The installed training submit hold and the original storage handoff checks remain required. The hold applies to the training submitter. A separate Pod admission fence covers every principal that can mount this one claim in the training namespace, including controller-created Pods and the optional owner. The same fence protects direct Pod references to the one transaction inventory Secret, including consumers that omit the PVC. An independently authorized policy owner installs this ValidatingAdmissionPolicy and its Deny binding, verifies admission is effective on the managed API, and retains exclusive control over changes. The management tools render and verify those resources. They do not install policy or grant RBAC.

Render the closed guard before enrollment. Its closed phase denies new Pods using `training/airaware-checkpoints`. Other claims and namespaces retain their existing admission behavior. Verify the current resource UIDs and generations and test an inadmissible claim Pod using a server-side dry-run before handing over exclusivity.

```bash
guard="$sky/public_flamingo_stage_guard.py"
python3 "$guard" --run "$run" --phase closed > "$review_dir/closed-guard.json"
jq .job "$packet" > "$review_dir/stage-job.json"
```

For an open phase, the policy owner uses the real UID of a suspended, reviewed Job to render the exact successor. Its policy embeds the approved post-defaulting Pod specification and binds the Job owner. A UID- and resourceVersion-preconditioned policy update, independently recorded UIDs/generations and admission dry-runs belong to the policy owner. Keep the same binding throughout. Never delete the closed guard to open a phase. The phase must be closed again before releasing writer exclusivity. Pin the live policy UID, binding UID and both generations with the transaction records.

### Conditional ownership request

Both leaves must exist or be approved for fresh directory creation by the kubelet. Check existing parent paths and authorize any necessary new path components explicitly. The retained Qwen digest and prior run state lie outside both mounts. When both leaves already satisfy the UID-1000 requirements, use the normal stager. If the owner establishes that fresh leaves need initialization, render the separately reviewable 60-second Job.

```bash
python3 "$sky/prepare-public-flamingo-leaves.py" render "${render[@]}" > "$owner_job"
sha256sum "$owner_job"
```

Obtain separate approval for that exact render, image, admission identity and CPU placement. The owner must verify that both targets are new and empty, the directory entries resolve to the intended leaves on the bound disk, the default service account has denied egress, and the existing workload hold remains active. The Job uses UID 0, a read-only root filesystem, no service account token, the single CHOWN capability, and two exact PVC subPath mounts. It checks both directories before changing the owner of a root-owned leaf to UID 1000. Group, file modes, descendants and siblings are outside the operation. A partial handoff can be reconciled using the same two empty leaves. The owner must use suspended enrollment, the owner-specific admission phase, and the exact-UID activation check described below. Retire the approved Job and Pods with UID preconditions, recheck detachment and exclusively hand the claim to the stager. The staging submit command never starts the owner Job.

Use a separate leaf-owner kubeconfig for the owner command. The independently approved ownership envelope uses `purpose: public-flamingo-prepare-leaves`, `action: enroll`, `fresh_empty_leaves: true`, `exclusive_storage_handoff: true`, the run, raw packet and owner Job file hashes (`packet_file_sha256`, `owner_job_file_sha256`), `deployment_sha256`, `cutoff`, `expires_at`, `leaf_operator`, `storage_operator`, `staging_operator` and the original `closed_guard` pins.

```bash
owner_args=(--packet "$packet" --owner-job "$owner_job" --promotion "$promotion" --binding "$binding" --scope "$scope")
bootstrap/nebius/with-credentials python3 "$sky/prepare-public-flamingo-leaves.py" enroll "${owner_args[@]}" \
  --approval "$leaf_approval" --approved-sha256 "$leaf_approval_sha256"
python3 "$guard" --run "$run" --phase owner --job "$owner_job" --job-uid "$owner_job_uid" --expires-at "$leaf_expiry" > "$review_dir/owner-guard.json"
```

The policy owner opens only the reviewed owner Pod and its real enrolled Job UID. The separate activation envelope repeats the ownership scope, sets `action: activate`, and includes `enrollment_approval_sha256`, `job_resource_version` and the live `owner_guard` pins including `job_uid` and the approved `expires_at`.

```bash
bootstrap/nebius/with-credentials python3 "$sky/prepare-public-flamingo-leaves.py" activate "${owner_args[@]}" \
  --enrollment-approval "$leaf_approval" --approval "$leaf_activation" --approved-sha256 "$leaf_activation_sha256"
```

Verify the owner Pod UID, admitted Pod specification, runtime image and completion. Close the guard on the same policy and binding, retire the exact owner writer and observe detach before the next phase.

### Issue the version-specific inventory

Before issuance, the enrolled storage observer verifies the closed guard on both sides of a full Pod scan and confirms the named Secret is absent. It rejects a preexisting consumer that could otherwise obtain a newly created Secret. Record the byte hash of this check in the independent source approval; the same closed guard UIDs and generations must remain effective through issuance.

```bash
python3 "$sky/check-public-flamingo-consumers.py" --run "$run" \
  --closed-pins "$closed_pins" --closed-pins-sha256 "$closed_pins_sha256" > "$consumer_check"
```

The source custodian verifies effective object-store authority for exactly the nine listed object versions and supplies the dedicated reader credential. It must lack list, write, other version and other object privileges. Review the GET version grant and pin its private record. The source operator uses a separately approved kubeconfig scoped to create the named immutable Secret; admission must restrict creates to its exact name and shape. Install and verify the transaction’s closed guard before issuing the Secret. Protect its name against replacement throughout the handoff. The staging and storage observers require Kubernetes `get` only on this one named Secret by `resourceNames`. That verb authorizes reading the whole Secret even when the tools request metadata. Use only those authorized identities and configure audit and admission logging to exclude Secret request and response bodies.

Set the enrolled `NEBIUS_TRAINING_CLUSTER_ID`, `NEBIUS_CA_BUNDLE_SHA256` and `NEBIUS_API_ENDPOINT`. For each command set `NEBIUS_SUBMIT_KUBECONFIG` to the private prod-nebius training context belonging to that command's independently authorized operator. Source approval includes the exact packet, effective grant, reader, identities and expiry. The credential file and grant stay on the source owner's host. The issuer creates the Secret once. Keep shell tracing and SDK debug logging off.

```bash
python3 "$sky/issue-public-flamingo-inventory.py" \
  --packet "$packet" --promotion "$promotion" --binding "$binding" --scope "$scope" \
  --approval "$source_approval" --approved-sha256 "$source_approval_sha256" \
  --consumer-check "$consumer_check" --reader-grant "$effective_grant" --reader-credentials "$private_reader" > "$issued"
```

The source approval records the observer receipt's `consumer_check_sha256` and pins the live `closed_guard` policy/binding UIDs and generations. The issuer reads those two named admission resources before signing and again before Secret creation. The source approval contains `purpose: issue-public-flamingo-inventory`, `packet_file_sha256`, `deployment_sha256`, `run`, `inventory_secret`, three distinct principal names (`source_operator`, `staging_operator`, `storage_operator`), `reader_grant_sha256`, `expires_at` and integer `ttl_seconds` from 1 to 1800. The private reader credential document contains `access_key_id`, `secret_access_key` and optionally `session_token`. The independently attested effective grant uses schema `public-flamingo-version-reader-grant-v1` and records `principal`, `access_key_id_sha256`, `project_id`, `objects_sha256`, `permissions: ["s3:GetObjectVersion"]`, `exact_versions_only: true` and `expires_at`. Both timestamps include a timezone. Issuance checks a one-byte GET for each promoted version.

For the original allocation, the model owner can also deliver nine pre-issued GET capabilities over a private channel. Keep the signing credentials in model-owner custody. The owner must be distinct from the source, storage and staging operators. This handoff attests to possession of the nine bearer capabilities. Their scope comes from the signed requests. Successor windows use the version reader grant bound to the signed window.

- The private inventory JSON has a single `base` array in the packet's promoted order. Each record has exactly `name`, `size`, `sha256`, `version_id` and `url`. Each URL uses the packet's HTTPS bucket and key, the signed `versionId`, and a host-only SigV4 GET for `eu-south1/s3`.
- The private attestation JSON has exactly `schema` set to `public-flamingo-scoped-get-attestation-v1`, `model_owner`, `project_id`, `objects_sha256`, `inventory_sha256` and a timezone-aware `expires_at`. Hash the raw inventory file for `inventory_sha256`. The independent source approval pins the raw attestation as `capability_attestation_sha256` in place of `reader_grant_sha256`. Each URL must be live and expire strictly before the scope, approval and attestation cutoffs. Its signed lifetime must be at most the approval's `ttl_seconds`.
- Keep the inventory and attestation in files owned by the source operator with mode `0600`. The issuer checks all nine values against the approved manifest, probes each URL with a direct one-byte GET, and checks the approved closed generation again before the create. Record only the returned namespace, name, UID and resourceVersion as issuance evidence.

```bash
python3 -B "$sky/issue-public-flamingo-inventory.py" \
  --packet "$packet" --promotion "$promotion" --binding "$binding" --scope "$scope" \
  --approval "$source_approval" --approved-sha256 "$source_approval_sha256" \
  --consumer-check "$consumer_check" --capability-attestation "$capability_attestation" \
  --capability-inventory "$capability_inventory" > "$issued"
```

The Secret contains only `inventory.json`. Its UID is an input to the storage handoff. Source credential material stays out of the Job, ConfigMap, approval packet and receipts. After expiry, a new attempt requires explicit reconciliation and a fresh inventory. Existing Secret names are never overwritten.

## Submit one approved CPU writer

Use the approved staging operator kubeconfig and the established storage observer environment (`NEBIUS_STORAGE_KUBECONFIG`, `NEBIUS_STORAGE_OPERATOR_IDENTITY`, and the ledger-independent read access needed by the Nebius disk handoff). Supply this owner-approved JSON structure in an owner-only file, replacing the example strings with independently verified values.

```json
{
  "purpose": "public-flamingo-stage",
  "action": "enroll",
  "closed_guard": {"policy_uid": "<UID>", "binding_uid": "<UID>", "policy_generation": 1, "binding_generation": 1},
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
bootstrap/nebius/with-credentials python3 "$sky/submit-public-flamingo-stage.py" enroll \
  --packet "$packet" --approval "$stage_approval" --approved-sha256 "$stage_approval_sha256" \
  --promotion "$promotion" --binding "$binding" --scope "$scope" > "$enrollment"
```

Enrollment leaves the Job suspended and returns its UID and the ConfigMap UID. The policy owner verifies the admitted enrollment, renders the phase bound to the actual Job, and installs it as the direct successor of the closed fence under the independent approval.

```bash
python3 "$guard" --run "$run" --phase stage --job "$review_dir/stage-job.json" \
  --job-uid "$(jq -er .job_uid "$enrollment")" --expires-at "$stage_expiry" > "$review_dir/stage-guard.json"
```

The activation approval repeats the approved scope and identities, sets `action` to `activate`, and adds `enrollment_approval_sha256`, `configmap_uid`, and `stage_guard`. The latter records the live guard's four identity/generation fields, `job_uid` and the activation approval’s absolute `expires_at`. At Pod creation the guard checks the API server’s creation timestamp against the approved latest start, reserving the full Job deadline and 120 seconds for closeout. The stage policy generation advances by one and the binding stays at the enrolled generation. The submitter verifies the actual Pod admission restriction and the suspended Job, then patches the exact UID/resourceVersion. Reconcile uncertain calls using the existing UIDs.

```bash
bootstrap/nebius/with-credentials python3 "$sky/submit-public-flamingo-stage.py" activate \
  --packet "$packet" --approval "$activation" --approved-sha256 "$activation_sha256" \
  --enrollment-approval "$stage_approval" --promotion "$promotion" --binding "$binding" --scope "$scope"
```

The worker verifies projected program, inventory, version response headers, bounded lengths and SHA-256 while streaming. It reopens and hashes every staged file. It uses the weights directory rename and publishes the pinned `SHA256SUMS` last as the completion marker. An interrupted run can resume verified bytes inside the same public leaf after fencing the old writer. Reusing a completed snapshot validates every byte again. The output check verifies UID 1000, emptiness and write access without leaving a named entry.

## Collect and fence

Wait for terminal evidence before retirement. Use the storage observer identity and the approved packet's canonical JSON hash from enrollment. The capture reads the one successful child Pod and compares its source, actual placement and image with enrollment. Retain its raw file hash independently.

```bash
capture_args=(--packet "$packet" --approval "$activation" --approved-sha256 "$activation_sha256" --scope "$scope"
  --promotion "$promotion" --binding "$binding" --expected-deployment "$deploy" --run "$run"
  --packet-sha256 "$(jq -er .packet_sha256 "$enrollment")"
  --job-uid "$(jq -er .job_uid "$enrollment")" --configmap-uid "$(jq -er .configmap_uid "$enrollment")"
  --inventory-uid "$(jq -er .inventory_uid "$enrollment")")
python3 "$sky/public-flamingo-stage-closeout.py" capture "${capture_args[@]}" > "$capture"
sha256sum "$capture"
```

After capture, the policy owner closes the admission fence as the direct successor on the same policy and binding UIDs. Record the independent closure approval with purpose `public-flamingo-stage-fence`, `stage_approval_sha256` (activation approval), canonical `packet_sha256`, raw `capture_sha256`, `storage_operator`, original `cutoff`, bounded `valid_until` and `closed_guard` containing the four live identity and generation fields. Verify the closed policy is effective and preserve its exclusion until a subsequent authorized handoff. The authorized owners retire the named Job (foreground propagation), its Pod, ConfigMap and inventory Secret using UID preconditions. Confirm each absence and keep alternate writers fenced. For any failed or partial run, collect failure evidence and reconcile the writer before reusing the leaf. Only the external fence command emits the staging receipt used by the baseline launcher.

```bash
bootstrap/nebius/with-credentials python3 "$sky/public-flamingo-stage-closeout.py" fence "${capture_args[@]}" \
  --capture "$capture" --capture-sha256 "$approved_capture_sha256" \
  --closure "$closure" --closure-sha256 "$closure_sha256" > "$staging_receipt"
```

Retain the enrollment, successful worker evidence, owner retirement records, and final receipt together. Subsequent GPU funding and baseline launch require their own authorizations.

## Retire the transaction guard for the baseline handoff

Retirement releases the Pod-level exclusion. The baseline owner must first accept the exact private staging receipt and its supporting evidence while the closed fence is still effective. Keep the original native training submit hold active throughout. The successor storage owner assumes exclusive custody at the first guard deletion. Record that acceptance, its expiry, the next authorized operation, the raw receipt SHA-256, and the policy and binding UIDs/generations in the retirement approval. Until that handoff is authorized the guard stays closed.

1. From the approved management host, re-run the external `fence` command with the same inputs. Compare the original and refreshed receipts' run, binding, weights, UID/emptiness facts, capture pin and closed guard identities. Observation times may advance. Confirm the accepted receipt is within its approved validity window.
2. Verify the staging and optional owner Jobs, child Pods, ConfigMap and source Secret are retired, outstanding source and staging grants have been revoked or expired, and the two exclusive leaf writers have lost their activation authority. Confirm there are no other principals or controllers that can use the claim outside the successor's controlled handoff. Require the original native hold and empty submit binding, no active consumers, the original PVC/PV/CSI UIDs and provider detachment. Transfer the writer lock under the independent successor approval before removing the transaction guard.
3. Use the independently enrolled, CA-pinned policy-owner kubeconfig for the two named resources. Its additional permissions are `get` and `delete` for exactly `public-flamingo-training-claim` on `validatingadmissionpolicies` and `validatingadmissionpolicybindings`. Phase installation and switching require separate create or exact-name update authority. Kubernetes RBAC cannot limit `create` by resource name, so use the existing authorized policy owner and its admission controls for installation. The storage and staging accounts receive no policy-write authority.

With the exclusive successor handoff held, capture fresh UID/resourceVersion pairs. Use the original managed endpoint pins and `auth whoami` against the independently approved policy-owner identity before these calls. Keep separate read-only storage and policy-owner contexts.

```bash
set -euo pipefail
umask 077
k=(kubectl --kubeconfig "$policy_owner_kubeconfig" --context prod-nebius --request-timeout=15s)
python3 "$sky/private_api.py" "${k[@]}"
test "$("${k[@]}" auth whoami -o jsonpath='{.status.userInfo.username}')" = "$approved_policy_owner"
name=public-flamingo-training-claim
api=/apis/admissionregistration.k8s.io/v1
for role in policy binding; do
  case "$role" in
    policy) kind=validatingadmissionpolicies ;;
    binding) kind=validatingadmissionpolicybindings ;;
  esac
  "${k[@]}" get "$kind" "$name" -o json > "$review_dir/$role.before.json"
  jq -en --slurpfile live "$review_dir/$role.before.json" --slurpfile receipt "$staging_receipt" --arg role "$role" '
    $live[0].metadata as $m | $receipt[0].fence_evidence.guard_after as $guard |
    select($m.uid == $guard[$role + "_uid"] and $m.generation == $guard[$role + "_generation"]
           and $m.deletionTimestamp == null and ($m.resourceVersion | length) > 0) |
    {apiVersion:"v1",kind:"DeleteOptions",propagationPolicy:"Foreground",preconditions:{uid:$m.uid,resourceVersion:$m.resourceVersion}}
  ' > "$review_dir/$role.delete.json"
done
```

The policy owner verifies those two captured specs against the exact closed render. The binding deletion transfers exclusion to the already accepted successor. The subsequent deletions target only the captured objects; any conflict or uncertain response requires readback before continuing.

```bash
jq -en --slurpfile expected "$review_dir/closed-guard.json" --slurpfile p "$review_dir/policy.before.json" \
  --slurpfile b "$review_dir/binding.before.json" \
  '$expected[0].items[0].spec == $p[0].spec and $expected[0].items[1].spec == $b[0].spec' >/dev/null
"${k[@]}" delete --raw "$api/validatingadmissionpolicybindings/$name" -f "$review_dir/binding.delete.json"
remaining=$("${k[@]}" get validatingadmissionpolicybinding "$name" --ignore-not-found -o name)
test -z "$remaining"
"${k[@]}" delete --raw "$api/validatingadmissionpolicies/$name" -f "$review_dir/policy.delete.json"
remaining=$("${k[@]}" get validatingadmissionpolicy "$name" --ignore-not-found -o name)
test -z "$remaining"
```

After retirement, verify the installed native hold and its Deny binding against `native-activation.py` (`held_policy_and_binding` and `check_active`), recheck both attachment layers with `training-storage-handoff.py`, and retain readbacks under the successor handoff. Use server dry-runs for the successor operator's exact baseline render and the denied ordinary submitter request. The accepted stage receipt records the completed, fenced writer. The retirement record identifies which owner now prevents another writer. A later baseline launch must recheck current writer custody, funding, placement, disk identity and its own admission grant.

For a separately approved allocation on the retained disk, use the [successor window procedure](public-flamingo-successor.md).
