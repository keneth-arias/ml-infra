# HAMi workload standby and acceptance

Apply the namespace, resource budget and admission policies together through the reviewed standby kustomization. HAMi intent is confined to the exact `hami-workloads` namespace. The default and workload service accounts have token automount disabled. The namespace uses Restricted Pod Security and denies network ingress and egress. The workload operator Role has no binding by default. Bind it only to the separately approved operator or trusted workload submitters. Keep namespace, quota, policy, Pod binding, Pod update, and credential management under platform ownership.

The zero GPU quota is the standby interlock. Leave it in place until Nebius reports a dedicated, funded Managed Kubernetes pool and the platform owner verifies its provider instance identity, runtime, isolation, and billing scope. Verify the pool is labeled `gpu=on` and `airaware.training/gpu-mode=hami`, has the dedicated `NoSchedule` taint, and advertises `hami.io/gpu` with zero `nvidia.com/gpu`. Inspect the node's system Pods for a native NVIDIA registrar, including static mirror Pods. Keep original whole-GPU training on its existing pool.

Trusted Pods and Job templates use the `hami-workload` service account, explicit token automount false, Restricted security settings, a bounded Pod deadline, and digest-pinned images. Choose the deadline inside the approved allocation and configure the Job deadline and retry policy as well. Each container supplies bounded CPU, memory and ephemeral-storage requests and limits. GPU containers use `hami.io/gpu`, `hami.io/gpucores`, and exactly one of `hami.io/gpumem` in MiB or `hami.io/gpumem-percentage`. Leave native GPU keys, custom allocator annotations, alternate schedulers, and device claims out of the template. Submitting a Job does not validate its eventual Pod or reserve any GPU. Inspect failed-create events if its Pod is denied. Isolation between mutually untrusted tenants requires a separate tenancy design.

## Two concurrent CUDA probes

Activate only after the reviewed HAMi release and distinct managed pool are operational. Review a finite quota increase for the exact test window. Two probes require a total of two virtual slots, 8192 MiB, and 50 core points. Keep percentage, priority, and native GPU quotas at zero. The existing CPU, memory, storage, and Pod quotas cover the two probes. Verify the ResourceQuota status reflects the approved limits before submitting.

The example uses the same immutable image digest and Conda interpreter as the completed signed qualification. It uses synthetic GPU matrices with no datasets, credentials, or retained files. The example is outside the kustomization. Use the workspace's pinned TLS and managed-identity check before any call. Run the following in the repo checkout from an approved operator session with the exact cluster and kubeconfig environment. The independently checked `kubectl` request identity must have the namespaced Role plus read-only access for node and cluster checks.

```bash
set -euo pipefail
umask 077
export NEBIUS_API_ENDPOINT=public
export NEBIUS_SUBMIT_KUBECONFIG="$HOME/.kube/nebius-prod"
# Load the existing owner-reviewed NEBIUS_TRAINING_CLUSTER_ID and NEBIUS_CA_BUNDLE_SHA256.
# Set HAMI_WORKLOAD_OPERATOR to the independently approved request username.
PYTHONPATH="$PWD/workloads/sky:$PWD/bootstrap/nebius" python3 - <<'PY'
import os
from production_target import kubectl_client_for_identity

# The approved kubeconfig is scoped to the training namespace for identity verification.
kubectl_client_for_identity("training", os.environ["HAMI_WORKLOAD_OPERATOR"], direct=True)
PY
k=(kubectl --kubeconfig "$NEBIUS_SUBMIT_KUBECONFIG" --context prod-nebius --namespace hami-workloads)
example=platform/hami/workloads/probe-pod.yaml.example

"${k[@]}" create --dry-run=server -f "$example" -o name
```

Server dry-run checks admission only. After both dry-run and the independent pool review pass, run the commands below during the approved test window. Use a new private directory. Creation receipts and the cleanup trap identify only this attempt's exact Pod UIDs. Preserve logs and Kubernetes Events when a Pod fails or remains Pending. Never retry blindly when the result of creation is ambiguous.

```bash
receipts=$(mktemp -d)
cleanup() {
  result=0
  for i in 1 2; do
    file="$receipts/create-$i.json"
    [[ -s "$file" ]] || continue
    name=$(jq -er .metadata.name "$file") || continue
    uid=$(jq -er .metadata.uid "$file") || continue
    jq -n --arg uid "$uid" '{apiVersion:"v1",kind:"DeleteOptions",preconditions:{uid:$uid}}' |
      "${k[@]}" delete --raw "/api/v1/namespaces/hami-workloads/pods/$name" -f - || {
        if ! remaining=$("${k[@]}" get pod "$name" --ignore-not-found -o name) || [[ -n $remaining ]]; then
          echo "Reconcile Pod $name with UID $uid" >&2
          result=1
        fi
      }
  done
  return "$result"
}
trap cleanup EXIT
for i in 1 2; do
  "${k[@]}" create -f "$example" -o json > "$receipts/create-$i.json"
done
for i in 1 2; do
  name=$(jq -er .metadata.name "$receipts/create-$i.json")
  if ! "${k[@]}" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" --timeout=210s; then
    "${k[@]}" get events --field-selector "involvedObject.name=$name" > "$receipts/events-$i.txt"
  fi
  "${k[@]}" get pod "$name" -o json > "$receipts/pod-$i.json"
  "${k[@]}" logs "$name" -c cuda-probe > "$receipts/log-$i.txt"
done
"${k[@]}" get node "$approved_hami_node" -o json > "$receipts/node.json"
python3 platform/hami/workloads/verify-overlap.py \
  --node "$receipts/node.json" --approved-provider-id "$approved_hami_provider_id" \
  --pod "$receipts"/pod-{1,2}.json --log "$receipts"/log-{1,2}.txt
```

Accept only two distinct successful Pods using the reviewed immutable image, one shared GPU UUID on the exact approved node, the allocator's 4096 MiB and 25% core slice receipts, a CUDA-reported memory ceiling, and at least 15 seconds of overlap between their active CUDA probe intervals. The core receipt verifies the assigned request. It does not measure a throughput percentage. Restore the reviewed zero GPU quotas after the experiment and confirm both Pod UIDs are gone and their reservations have been reclaimed.

For a policy regression check, verify the live policy has zero type-check warnings. After the policies are installed, expect server dry-run to reject the example under standby quota and reject any out-of-namespace HAMi scheduler, HAMi limits, or reserved annotation. With the approved test quota, also check forged allocator annotations and direct node assignments in the pilot namespace and confirm a known-good ordinary Pod without HAMi intent still passes its pre-existing admission controls.
