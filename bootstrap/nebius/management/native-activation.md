# Open and close the native training submitter

The default production overlay holds `training:episode-submit` closed. The operator-only transaction opens one attended session during a reviewed support or GPU window. In support mode actual training submissions are limited to CPU preflight. The verification probe can dry-run the signed GPU Job shape. GPU mode also admits the signed GPU Job under the existing workload policy.

## Verify the deployment and window

Use the exact reviewed deployment checkout on bm, an approved managed operator identity and its independently pinned public kubeconfig. Verify the signed production release and independently approved deployment SHA-256. Confirm the guarded worker allocation, Ready Kubernetes/provider identities, cutoff, ledger and retained storage receipts. Require just the approved support node in a support window. Require the approved support and GPU nodes, successful support and placement checks, and sufficient budget in a GPU window. Keep both worker shutdown schedulers armed.

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

The intent and per-operation checkpoints identify the attempted marker, policy, Role and RoleBinding. Keep them available if a create response was lost or the process crashed. The wrapper only reconciles resources with the originating attempt label and recorded UID, or the matching object recovered from an in-flight create without a UID response. It uses UID-precondition deletes and a resource-version precondition for the policy. It attempts the binding removal, marker revocation and held-policy restoration even when another step fails. It verifies token rejection before removing the local kubeconfig. Keep the private directory until closeout succeeds. An ownership mismatch requires operator reconciliation, and the wrapper leaves the unexpected object intact.

The `check-held` command compares the live policy and binding on standard input with the canonical held configuration. It is a read-only diagnostic. If the managed API becomes unreachable, keep the worker shutdown schedulers armed, preserve the session directory and reconcile the grant, policy and workloads as soon as the API is reachable. Reconcile outstanding Jobs before ordinary worker release.
