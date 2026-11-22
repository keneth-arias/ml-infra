# Open and close the native training submitter

The default production overlay holds `training:episode-submit` closed. The operator-only transaction opens one attended session during a reviewed support or GPU window. In support mode actual training submissions are limited to CPU preflight. The verification probe can dry-run the signed GPU Job shape. GPU mode also admits the signed GPU Job under the existing workload policy.

With a signed v1 release, both stages activate only the Qwen pair and deny Flamingo submissions, including dry runs. With a signed v2 release, activation verifies both pairs against the checked-in policy.

## Verify the deployment and window

Use the exact reviewed deployment checkout on bm, an approved managed operator identity and its independently pinned public kubeconfig. Verify the signed production release and independently approved deployment SHA-256. Confirm the guarded worker allocation, Ready Kubernetes/provider identities, cutoff, ledger and retained storage receipts. Require just the approved support node in a support window. Require the approved support and GPU nodes, successful support and placement checks, and sufficient budget in a GPU window. Keep both worker shutdown schedulers armed.

For both release versions, `current_release` verifies the signed records under `/etc/ml-infra/nebius-release/current` and matches `deployment_commit` to the exact checkout. Keep using the existing enrolled checkout for Qwen until a signed v1 or v2 deployment enrolls the new checkout. Close and fence the existing session to restore the canonical hold before switching the checkout and release.

Reconcile older sessions first. Review other effective grants to `training:episode-submit`, including namespace and cluster bindings, group grants and grants limited to named resources. The held and released probes check effective access, including the denial of direct Pod creation, exec, port-forward, Secret reads and token creation. Use the existing approved policy field manager. The transaction needs exclusive attended control of the native policy and grant during activation and rollback. Schedule and record closure before activation.

## Activate one bounded session

From the repository root, with a private parent directory on the management host:

```bash
set -euo pipefail
umask 077
source ~/.config/ml-infra/nebius-k8s-public.env
export NEBIUS_API_ENDPOINT=public
tool=bootstrap/nebius/management/native-activation.py
operator="$HOME/.kube/nebius-prod"
session="$private_parent/native-window-$(date -u +%Y%m%dT%H%M%SZ)"
stage=$(jq -er '.stage | select(. == "support" or . == "gpu")' "$approved_window_scope")
uv run "$tool" activate --stage "$stage" --scope "$approved_window_scope" \
  --expected-receipt "$approved_deployment_sha256" --operator-config "$operator" \
  --operator-identity "$approved_operator" --field-manager "$policy_manager" --session "$session"
export NEBIUS_SUBMIT_KUBECONFIG="$session/kubeconfig"
```

The wrapper verifies the target endpoint, CA, operator identity, active scope and signed deployment. It writes the attempt intent before calling the managed API. An immutable empty Secret carries the attempt label and binds the short-lived token to its exact UID. The wrapper builds the private kubeconfig, validates expiry against the cutoff and confirms both the exact submit identity and the held probe before changing the policy. After the activated policy and effective Deny binding are observed, it rechecks the signed release and token lifetime, then creates the attempt-owned RoleBinding and runs the released-mode probe. A successful call prints the kubeconfig path. Keep the credential on bm and use it only with the trusted attended operator workflow.

The policy controls API admission and the credential grants the stated namespaced API permissions. Within those permissions the raw API has no per-fit funding check. Use the [guarded launcher and fit reservation](../../../workloads/sky/launch-episode.sh) on bm for real preflight and GPU submissions. Verify the approved placement, tracking and funding receipts and remaining deadline before each launch. Keep raw submissions closed to other clients. Use the trusted operator for placement observations and cancellation. Closing the grant does not cancel existing Jobs, which must be reconciled through the workload closeout.

## Close or recover the original session

Close promptly when submissions finish, before the token expires and before the worker cutoff. On a failed activation the wrapper attempts closeout itself. If its process was interrupted or any step remains unconfirmed, rerun close with the original directory and independently pinned operator environment:

```bash
uv run "$tool" close --operator-config "$operator" --session "$session"
unset NEBIUS_SUBMIT_KUBECONFIG
```

The intent and per-operation checkpoints identify the attempted marker, policy, Role and RoleBinding. Keep them available if a create response was lost or the process crashed. The wrapper only reconciles resources with the originating attempt label and recorded UID, or the matching object recovered from an in-flight create without a UID response. It uses UID-precondition deletes. The rollback writes a unique fence annotation under an exact resource-version precondition and checks the hold and new version, including when the policy was already held. A delayed activation patch using the old version then fails. It attempts the binding removal, marker revocation and held-policy restoration even when another step fails. It verifies token rejection before removing the local kubeconfig. Keep the private directory until closeout succeeds. An ownership mismatch requires operator reconciliation, and the wrapper leaves the unexpected object intact.

If a create started and its response was lost, an absent object leaves the session unresolved. Retry the same close command to catch a subsequently visible object. If the object never appears, reconcile the exact create with the managed Kubernetes API audit. Require a correlated `ResponseComplete` event for the original operator, namespace, resource, name and attempt, with a definitive rejection status. An absent audit event, timeout or server-side 5xx leaves the outcome uncertain. With the original event retained in the protected audit location, record the verified result in the owner-only session file `marker-no-create.json`, `role-no-create.json` or `binding-no-create.json`, respectively. Use these fields:

| Field | Value |
| --- | --- |
| `attempt`, `target` | Exact values from the session's `intent.json` |
| `resource` | `secrets`, `roles` or `rolebindings` |
| `name` | Exact intended object name from the session's intent |
| `stage`, `verb` | `ResponseComplete`, `create` |
| `audit_id` | The original event's Kubernetes audit UUID |
| `status_code` | The event's recorded 400, 401, 403, 404, 409 or 422 response |

Run close again. It checks live ownership first even when audit evidence is present. The completion checkpoint is `closed-reconciled.json`.

The `check-held` command compares the live policy and binding on standard input with the canonical held configuration. It is a read-only diagnostic. If the managed API becomes unreachable, keep the worker shutdown schedulers armed, preserve the session directory and reconcile the grant, policy and workloads as soon as the API is reachable. Reconcile outstanding Jobs before ordinary worker release.

## Flamingo direct forecast fits

Build the committed OpenTSLM forecast code and private reader with the paired image builder. Set `FORECAST_BACKBONE=flamingo` for this build and supply both trainer and preflight image tags. This selects the Gemma 3 runtime. Promote both OCI digests through the existing signed deployment and native admission process on the verified Nebius managed cluster.

Pass `--flamingo-binding-sha256` with the digest exported by AirAware to the staging Job and to `launch-episode.sh --nebius job fit direct`. Use the binding’s original fit and inner-validation manifest hashes and its new base weights hash for the cache. The worker reconstructs and checks the exact binding, rehashes the mounted base, verifies both original cohort manifests and supplies the binding file to the trainer. Provision an independent funded fit request for these artifact and render hashes. Use the seed screen, cooperative shutdown, tracking and checkpoint recovery flow described above.

Before a funded Flamingo fit, run the managed baseline and promote its evidence as described in the [baseline handoff](../../../workloads/smoke/flamingo-baseline.md). Provide `--flamingo-baselines FIRST_SHA REPEAT_SHA --flamingo-parity-sha256 PARITY_SHA` to the launcher. The funded request binds the ordered language receipt digests and independently approved reference parity digest. The worker verifies the parity report, freeze, pair, base and runtime before training. At terminal selection it collects two independent restores, verifies language and numeric outputs against the approved baselines, and publishes the receipts and comparison to MLflow. Allow time for this work in the approved deadline.

For a zero-progress retry, fence and quarantine the previous writer’s state, prepare an empty run directory and record the existing `fit_attempt.py check-fresh` attestation. Promote the same approved parity report, freeze and language receipt pair into that directory after the empty-state check and before launching the fresh attempt.
