# Stage HAMi with production Flux

Use the approved managed Nebius operator context on bm and a funded Ready CPU worker. Verify the cluster ID, API endpoint, TLS CA and operator identity using the same pinned checks as the production Flux pilot. Keep the existing platform reconciliation in place. The separate `nebius-hami` Kustomization uses the installed source and Kustomize controllers and the existing read-only repository key.

Before applying, review the merged Git commit and the complete `platform/hami/system` build. Check that the reviewed DaemonSet node selector matches zero live nodes. The current Nebius GPU pool uses the managed NVIDIA plugin and stays outside HAMi. Verify that the core points to the operator-owned TLS Secret `hami-system/hami-scheduler-tls` and that the certificate covers `hami-scheduler.hami-system.svc`. Stage the approved namespaces, phase guard, Secret and the exact `runtime-access.yaml` from the pinned commit through the operator's separate bootstrap. Keep the opt-in webhook absent until the scheduler is healthy. The Secret, runtime RBAC and webhook belong to the operator.

Render first and save the outputs for review. The renderer requires the full commit and a local `origin/main` containing it. It builds the core from the commit bytes. Supply a new owner-only output directory.

~~~bash
set -euo pipefail
umask 077
k=(kubectl --kubeconfig "$HOME/.kube/nebius-prod" --context prod-nebius --request-timeout=15s)
render=(uv run --offline bootstrap/nebius/management/hami/render-flux.py "$commit")
manager=nebius-hami-bootstrap
"${render[@]}" access >"$review/access.json"
"${render[@]}" core >"$review/core.json"
"${render[@]}" bootstrap-core >"$review/dormant.json"
"${render[@]}" stage >"$review/stage.json"
"${k[@]}" apply --server-side --dry-run=server --field-manager="$manager" -f "$review/access.json"
"${k[@]}" create --dry-run=server --field-manager="$manager" -f "$review/dormant.json"
"${k[@]}" apply --server-side --dry-run=server --field-manager="$manager" -f "$review/stage.json"
~~~

For first creation, the approved operator applies the additive access and suspended stage, then creates the reviewed dormant objects so that Flux only needs named `get` and `patch` permissions. The dormant scheduler has zero replicas. Inspect any object that already exists and confirm its ownership before continuing. The existing controller Role, source, Kustomization and three-object inventory require no change.

~~~bash
"${k[@]}" apply --server-side --field-manager="$manager" -f "$review/access.json"
"${k[@]}" apply --server-side --field-manager="$manager" -f "$review/stage.json"
"${k[@]}" create --field-manager="$manager" -f "$review/dormant.json"
account=system:serviceaccount:flux-system:nebius-hami
controller=system:serviceaccount:flux-system:kustomize-controller
"${k[@]}" --as="$controller" -n flux-system auth can-i impersonate serviceaccounts/nebius-hami
"${k[@]}" --as="$controller" -n flux-system auth can-i patch kustomizations/nebius-hami --subresource=status
"${k[@]}" --as="$account" -n hami-system auth can-i patch deployments/hami-scheduler
require_denied() {
  local result
  local status=0
  result=$("${k[@]}" --as="$account" "$@") || status=$?
  [[ "$status" == 1 && ( "$result" == no || "$result" == "no - "* ) ]]
}
require_denied -n hami-system auth can-i get secrets/hami-scheduler-tls
require_denied -n hami-system auth can-i create deployments
require_denied -n sky-cpu auth can-i patch resourcequotas/pilot
require_denied auth can-i patch clusterroles
"${k[@]}" --as="$account" apply --server-side --dry-run=server --force-conflicts \
  --field-manager=kustomize-controller -f "$review/core.json"
~~~

Enable only the named Git source after checking the scoped apply and reaffirming that zero GPU nodes match the DaemonSet selector. Before applying `enable.json`, review the source artifact revision, the suspended spec, and the resource-version precondition. The first Flux reconciliation starts the scheduler on CPU.

~~~bash
src="gitrepository.source.toolkit.fluxcd.io/hami-$commit"
ks=kustomization.kustomize.toolkit.fluxcd.io/nebius-hami
"${render[@]}" stage --fetch |
  jq -e '.items[0]' >"$review/fetch.json"
"${k[@]}" apply --server-side --field-manager="$manager" -f "$review/fetch.json"
"${k[@]}" -n flux-system wait --for=condition=Ready "$src" --timeout=3m
"${k[@]}" -n flux-system get "$src" -o json >"$review/source-live.json"
"${k[@]}" -n flux-system get "$ks" -o json >"$review/stage-live.json"
"${render[@]}" enable --source-readback "$review/source-live.json" \
  --kustomization-readback "$review/stage-live.json" >"$review/enable.json"
"${k[@]}" apply --server-side --dry-run=server --field-manager="$manager" -f "$review/enable.json"
"${k[@]}" apply --server-side --field-manager="$manager" -f "$review/enable.json"
"${k[@]}" -n flux-system wait --for=condition=Ready "$ks" --timeout=5m
"${k[@]}" -n flux-system get "$ks" -o json >"$review/active-live.json"
jq -e --arg revision "$(jq -er .status.artifact.revision "$review/source-live.json")" \
  --slurpfile core "$review/core.json" '
  .status.lastAppliedRevision == $revision and .status.observedGeneration == .metadata.generation
  and ([.status.inventory.entries[].id] | sort) ==
      ([$core[0].items[] | [.metadata.namespace, .metadata.name,
        (if .apiVersion | contains("/") then .apiVersion | split("/")[0] else "" end),
        .kind] | join("_")] | sort)' "$review/active-live.json"
"${k[@]}" -n hami-system rollout status deployment/hami-scheduler --timeout=3m
"${k[@]}" -n hami-system get daemonset hami-device-plugin -o json |
  jq -e '.status.observedGeneration >= .metadata.generation
    and .status.desiredNumberScheduled == 0 and .status.currentNumberScheduled == 0'
~~~

Then the operator verifies the serving certificate and Service endpoint before applying the exact `operator/webhook.yaml` template with the separately verified public `caBundle`. Verify the opt-in namespace selector and the webhook's effect using the approved workload probes. Recheck the existing platform Kustomization inventory and readiness.

For a later core upgrade, review the immutable replacement source and diff against the live objects. If resource identities change, stage the newly named grant and create the newly reviewed dormant objects before switching the Kustomization source. Retire grants only after verifying the active inventory and intended rollback. For rollback, keep the workload gate closed, retire or disable the webhook under the operator identity, suspend `nebius-hami` and verify reconciliation has stopped before scaling down or restoring core to the previous reviewed revision. Suspending alone leaves installed objects running. Pruning and forced replacement are disabled.

Run the local handoff tests with `python3 bootstrap/nebius/management/hami/test_render_flux.py`.
