# Open and close the native training submitter

The normal production overlay holds `training:episode-submit` closed. Apply this opt-in procedure only from a separately reviewed merged release, during the approved funded support or GPU window. The support policy permits actual CPU preflight Jobs and lets the probe dry-run the signed GPU Job shape. A GPU-stage policy requires the approved GPU window and the successful support and placement evidence. Both stages keep the same namespace, signed image mapping, workload account and other admission validations.

## Verify the deployment and worker window

Work from the exact deployment checkout on bm. Use the signed production release and a deployment SHA-256 from the independently reviewed approval. Reconcile the previous pilot's controller and credential closeout. Verify the worker allocation proof, hard stop, ledger, retained volumes, and Ready provider-backed worker identities with the guarded window procedure. For support, require the approved support node and no GPU nodes. For GPU, require the approved support and GPU node, plus the intended preflight, tracking, allocation and positive remaining-budget receipts. Keep both worker cleanup schedulers armed.

```bash
set -euo pipefail
umask 077
source ~/.config/ml-infra/nebius-k8s-public.env
export NEBIUS_API_ENDPOINT=public
operator="$HOME/.kube/nebius-prod"
k=(kubectl --kubeconfig "$operator" --context prod-nebius --request-timeout=15s)
tool=bootstrap/nebius/management/native-activation.py
python3 workloads/sky/private_api.py "${k[@]}"
test "$("${k[@]}" auth whoami -o jsonpath='{.status.userInfo.username}')" = "$approved_operator"

stage=$(jq -er '.stage | select(. == "support" or . == "gpu")' "$approved_window_scope")
test "$(jq -er .cluster_id "$approved_window_scope")" = "$NEBIUS_TRAINING_CLUSTER_ID"
start=$(date -u -d "$(jq -er .start "$approved_window_scope")" +%s)
cutoff=$(date -u -d "$(jq -er .cutoff "$approved_window_scope")" +%s)
now=$(date -u +%s)
(( start <= now && now + 600 < cutoff ))
work=$(mktemp -d "$private_parent/native-window.XXXXXXXX")
trainer=$(python3 workloads/sky/production_release.py reference trainer)
preflight=$(python3 workloads/sky/production_release.py reference preflight)
policy=validatingadmissionpolicy/episode-submit-inputs
guard=validatingadmissionpolicybinding/episode-submit-inputs
role=episode-native-window

"${k[@]}" get "$policy" "$guard" -o json > "$work/held-live.json"
jq -e '.items[] | select(.kind == "ValidatingAdmissionPolicy")' "$work/held-live.json" |
  python3 workloads/sky/production_release.py check-native-policy > "$work/held-receipt.txt"
test "$(cat "$work/held-receipt.txt")" = "$approved_deployment_sha256"
uv run "$tool" check-held < "$work/held-live.json"
uv run "$tool" held-policy > "$work/held.json"
uv run "$tool" policy --stage "$stage" --expected-receipt "$approved_deployment_sha256" --scope "$approved_window_scope" > "$work/active.json"
```

Check the live policy's first validation is the held deny and the live binding enforces it for `training`. Review the rendered policy and confirm both staged images and every validation apart from the intended hold transition match the reviewed source. Resolve all other effective grants to the native account before proceeding, including resource-name and group grants across RoleBindings and ClusterRoleBindings. In particular inspect grants for `pods/exec`, `pods/portforward`, Secrets and ServiceAccount token creation. Record the actual field manager of the staged policy and reconcile owned fields under that manager. This is an attended change. Do not run a concurrent reconciliation of the held policy while activating.

For rollback, save the workspace and schedule closure before changing the policy. Use the earlier of the planned end of submissions and the verified worker cutoff, with enough time to close credentials, finish approved Jobs and verify rollback. Record the scope, expected receipt, signed references, planned closure and operator logs together. Before switching from support to GPU, close the support grant and complete the return to the held state.

## Activate policy, then grant access

Review the server-side dry run. Use the established field manager `$policy_manager`; resolve conflicts before proceeding. Apply the activation while the window RoleBinding is absent. Re-read the whole policy and binding. The access renderer requires the exact reviewed policy, deployment receipt, stage, effective Deny binding and successful CEL status for the live generation. Poll read-only until the observed generation catches up. Any other mismatch keeps access closed.

```bash
live_role=$("${k[@]}" -n training get role "$role" --ignore-not-found=true -o name)
live_binding=$("${k[@]}" -n training get rolebinding "$role" --ignore-not-found=true -o name)
test -z "$live_role" && test -z "$live_binding"
"${k[@]}" apply --server-side --dry-run=server --validate=strict --field-manager="$policy_manager" -f "$work/active.json"
"${k[@]}" apply --server-side --validate=strict --field-manager="$policy_manager" -f "$work/active.json" > "$work/activation.txt"
"${k[@]}" get "$policy" "$guard" -o json > "$work/active-live.json"
uv run "$tool" access --stage "$stage" --expected-receipt "$approved_deployment_sha256" --scope "$approved_window_scope" \
  < "$work/active-live.json" > "$work/access.json"
"${k[@]}" create --dry-run=server --validate=strict -f "$work/access.json"
jq -e '.items[] | select(.kind == "Role")' "$work/access.json" > "$work/role.json"
jq -e '.items[] | select(.kind == "RoleBinding")' "$work/access.json" > "$work/binding.json"
"${k[@]}" create --validate=strict -f "$work/role.json" -o jsonpath='{.metadata.uid}' > "$work/role.uid"
"${k[@]}" create --validate=strict -f "$work/binding.json" -o jsonpath='{.metadata.uid}' > "$work/binding.uid"
```

Create a short-lived credential for the exact account. Keep the kubeconfig on the trusted management host. A fresh immutable empty Secret binds the token to its own UID. Independently verify the returned expiry leaves time for the intended submissions and never extends beyond the authorized cutoff. If issuance or validation fails, keep credentials private, close the grant and reconcile the marker. Use a fresh session for a later funded interval.

```bash
marker="episode-native-window-$(date -u +%Y%m%d%H%M%S)"
printf '{"apiVersion":"v1","kind":"Secret","metadata":{"name":"%s","namespace":"training"},"immutable":true}\n' "$marker" |
  "${k[@]}" create --validate=strict -f - -o jsonpath='{.metadata.uid}' > "$work/marker.uid"
marker_uid=$(cat "$work/marker.uid")
"${k[@]}" -n training create token episode-submit --duration=10m --bound-object-kind=Secret \
  --bound-object-name="$marker" --bound-object-uid="$marker_uid" -o json > "$work/token.json"
expiry=$(date -u -d "$(jq -er .status.expirationTimestamp "$work/token.json")" +%s)
(( $(date -u +%s) < expiry && expiry <= cutoff ))
"${k[@]}" config view --minify --flatten --raw -o 'jsonpath={.clusters[0].cluster}' > "$work/transport.json"
jq -n --slurpfile transport "$work/transport.json" --slurpfile issued "$work/token.json" '
  {apiVersion:"v1",kind:"Config",clusters:[{name:"prod-nebius",cluster:$transport[0]}],
   contexts:[{name:"prod-nebius",context:{cluster:"prod-nebius",user:"episode-submit",namespace:"training"}}],
   "current-context":"prod-nebius",users:[{name:"episode-submit",user:{token:$issued[0].status.token}}]}
' > "$work/kubeconfig"
export NEBIUS_SUBMIT_KUBECONFIG="$work/kubeconfig"
python3 workloads/smoke/verify-training-access.py --trainer-image "$trainer" --preflight-image "$preflight" > "$work/probe-active.txt"
```

Leave access closed on any failed check. Before each real launch, verify the same funded window and remaining deadline, operator placement and tracker receipts and the signed deployment. Use [the guarded launcher](../README.md#credentials-and-plan) for approved preflights and GPU work. The activation grant cannot replace the per-fit ledger reservation. Use the operator for placement observations, cancellation and cleanup. The submitter can create and observe training Jobs, read pod metadata and logs, and cannot mutate existing Jobs or create Pods. Keep the signed receipt associated with the submitted work.

## Return to the hold

At the earlier closure time, or immediately on any mismatch, remove the RoleBinding first. This fences submissions while the policy is restored. Treat an interrupted activation as open until the binding is confirmed absent. If creation lost its response, compare the live Role or RoleBinding with the saved rendered object and signed receipt before recovering its UID. Leave an unexpected owner or object for reconciliation. Existing Jobs require separate reconciliation by the scoped cleanup operator. Review names and UIDs before cancelling them; closing access does not terminate Jobs already accepted. Do not release workers until the usual workload and cloud cleanup steps have run.

```bash
delete_owned() {
  local group=$1 resource=$2 name=$3 uid_file=$4 uid live
  live=$("${k[@]}" -n training get "$resource" "$name" --ignore-not-found=true -o jsonpath='{.metadata.uid}')
  [[ -n $live ]] || return 0
  uid=$(cat "$uid_file")
  [[ $live == "$uid" && $uid =~ ^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$ ]] || return 1
  jq -n --arg uid "$uid" '{apiVersion:"v1",kind:"DeleteOptions",preconditions:{uid:$uid}}' |
    "${k[@]}" delete --raw "$group/namespaces/training/$resource/$name" -f - > /dev/null
}
delete_owned /apis/rbac.authorization.k8s.io/v1 rolebindings "$role" "$work/binding.uid"
live_binding=$("${k[@]}" -n training get rolebinding "$role" --ignore-not-found=true -o name)
test -z "$live_binding"
"${k[@]}" apply --server-side --validate=strict --field-manager="$policy_manager" -f "$work/held.json"
"${k[@]}" get "$policy" "$guard" -o json > "$work/closed-live.json"
uv run "$tool" check-held < "$work/closed-live.json"
python3 workloads/smoke/verify-training-access.py > "$work/probe-closed.txt"
delete_owned /apis/rbac.authorization.k8s.io/v1 roles "$role" "$work/role.uid"
delete_owned /api/v1 secrets "$marker" "$work/marker.uid"
python3 - "$operator" "$work/kubeconfig" <<'PYTHON'
from pathlib import Path
import os
import runpy
import sys

functions = runpy.run_path("bootstrap/nebius/management/cpu-session.py")
target = {"cluster_id": os.environ["NEBIUS_TRAINING_CLUSTER_ID"], "endpoint": os.environ["NEBIUS_API_ENDPOINT"]}
client = ["kubectl", "--kubeconfig", sys.argv[1], "--context", "prod-nebius"]
ca = functions["pinned_kubeconfig_ca"](client, target["cluster_id"], os.environ["NEBIUS_CA_BUNDLE_SHA256"],
                                       endpoint=target["endpoint"], direct=True)
functions["wait_unauthorized"](target, ca, Path(sys.argv[2]))
print("Managed API returned HTTP 401 for the revoked credential")
PYTHON
rm -- "$work/token.json" "$work/kubeconfig"
```

If the readback shows a stale CEL generation, poll the live objects until the current generation is observed before running the held probe. The held probe uses the still-valid scoped credential to confirm denied access. If it has expired, issue a fresh short-lived marker-bound verification credential while the RoleBinding stays absent. Revoke each marker with its original UID as the API delete precondition. Confirm each corresponding kubeconfig returns HTTP 401 on SelfSubjectReview and remove all delivered token and kubeconfig copies. Keep the verified deployment, hold readback and revocation receipts in the protected work directory. If the API is unreachable, keep worker shutdown scheduled and reconcile the binding, Jobs and marker as soon as the managed API is reachable.
