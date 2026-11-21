# Funded retained volumes for Nebius

The default production and tracking roots leave storage acquisition to the operator. The opt-in `db` target creates the Immediate/Retain class and `tracking/postgres` (16 GiB). The separate `training` target creates `training/airaware-checkpoints` and `training/airaware-model-state` (64 GiB each). The Compute CSI driver provisions an ext4 Network SSD when a claim is created, before any Pod consumes it. This starts billing on a worker-free cluster. [Nebius disk CSI](https://docs.nebius.com/kubernetes/storage/disk-over-csi.md)

Keep the original `airaware-network-retain` StorageClass for existing WaitForFirstConsumer volumes. The two operator release targets are the sole source of new claims for these names. If any already exist, stop this acquisition and review their bound PV, disk, ownership and retention under a separate compatibility plan. Existing PVCs and their immutable storage class stay in place. Apply the operator manifests without pruning. The production Flux root does not reference either release target, and older training claims carry the prune-disabled annotation.

## Prepare approval and record the initial inventory

Verify the specific managed Kubernetes endpoint, CA, kubeconfig context and operator identity. Create the canonical `training` and restricted `tracking` namespaces from the reviewed production and tracking configuration before storage acquisition. Apply only the namespace and account manifests at this step. Verify any prior worker hold is closed, its Terraform backend lock is gone, and both earlier cleanup schedulers are retired. Keep the dated retirement and state/ledger receipts with the acquisition evidence.

Fund storage before applying any claim. Verify effective tenant quota for five simultaneously existing disks (three retained and two 128 GiB worker boots), plus sufficient Network SSD capacity. Record USD cost from the earliest authorized provisioning time through the approved retention deadline, including any detached time, reserve for taxes, and an explicitly funded fallback if a bind fails. Enter the current price **per GiB-hour**, a reviewed USD non-GPU allocation, a named retention owner, the fail-by date for acquisition, and the retention deadline in an owner-only approval file. The funding owner reconciles the allocation against the cumulative $100 non-GPU and $250 total limits and archives the exact approved bytes. A sample shape follows. All values are synthetic.

```json
{
  "reservation_id": "retained-storage-example",
  "tenant_id": "tenant-example",
  "project_id": "project-example",
  "cluster_id": "mk8scluster-example",
  "approved_at": "2026-10-01T20:00:00Z",
  "acquisition_deadline": "2026-10-01T21:00:00Z",
  "retain_until": "2026-10-31T21:00:00Z",
  "ssd_gib_hourly_usd": "0.00009726",
  "tax_reserve_rate": "0.22",
  "allocated_non_gpu_usd": "13.00",
  "retention_owner": "Example storage operator"
}
```

Use a private working directory, one approval for both stages, and the same pinned CLI options throughout. Set `profile` to the independently approved Nebius profile, `endpoint` to the verified public or private endpoint mode, and `ca_sha256` to the independently recorded CA digest. Check the exact rendered manifests. The reservation label on the claims lets the capture distinguish the approved acquisition from a pre-existing claim. The separate DB and training manifests never redefine a claim.

```bash
umask 077
root=platform/retained-storage
k=(kubectl --kubeconfig "$kubeconfig" --context "$context")
check=(python3 "$root/check.py" --approval "$approval" --kubeconfig "$kubeconfig"
       --context "$context" --endpoint "$endpoint" --ca-sha256 "$ca_sha256" --profile "$profile")
reservation=$(jq -er .reservation_id "$approval")
"${check[@]}" preflight --output "$work/preflight.json"
for phase in db training; do
  kubectl kustomize "$root/$phase" |
    kubectl label --local -f - "airaware.storage/reservation=$reservation" -o yaml > "$work/$phase.yaml"
done
"${k[@]}" apply --server-side --dry-run=server --validate=strict -f "$work/db.yaml"
```

Preflight requires no workers, claims, PVs, attachments or Compute disks. The API may expose only the CSI node DaemonSet, with zero Ready Pods. The first actual claim is the worker-free controller qualification. A successful server dry run or driver registration cannot establish that provisioning works.

## Qualify the database claim first

Submit only the reviewed database manifest. Capture its Bound evidence within five minutes of claim creation before applying the two training claims. A four-minute `wait` leaves a minute for provider reconciliation and evidence capture. If the apply, wait or evidence check fails, stop acquisition. Inspect the named claim events and provider inventory to reconcile any asynchronous creation before retrying. Keep the storage commitment open for any disk that remains. Deleting a `Retain` claim alone leaves a billable disk, so use the retention owner's separately approved exact-ID closeout if the stage is abandoned.

```bash
"${k[@]}" apply --server-side --validate=strict --field-manager=nebius-retained-storage-operator -f "$work/db.yaml"
"${k[@]}" -n tracking wait --for=jsonpath='{.status.phase}'=Bound pvc/postgres --timeout=4m
"${check[@]}" db --output "$work/db-bound.json"
```

After the DB capture passes, review the training manifest against the live API, submit it once, and wait within the approved acquisition window.

```bash
"${k[@]}" apply --server-side --dry-run=server --validate=strict -f "$work/training.yaml"
"${k[@]}" apply --server-side --validate=strict --field-manager=nebius-retained-storage-operator -f "$work/training.yaml"
"${k[@]}" -n training wait --for=jsonpath='{.status.phase}'=Bound \
  pvc/airaware-checkpoints pvc/airaware-model-state --timeout=4m
"${check[@]}" all --db-receipt "$work/db-bound.json" --baseline "$work/inventory.json" --output "$work/all-bound.json"
sha256sum "$work/inventory.json" "$work/all-bound.json"
```

The checker requires the original DB binding, namespace UIDs and approval hash. It matches each final PVC UID to the Bound PV claim reference, then the CSI handle to an exact Ready and detached provider disk in the workload project. It records the PV UID, disk creation time and any CSI topology. If the provisioner emits an unrecognized handle or filesystem encoding, halt until there is an independently verified provider mapping. A Bound claim proves provisioning only. Check CSI node registration and PV topology, actual ext4 mount, writer ownership and detach/reattach during the funded worker qualification. PostgreSQL stays on the CPU support node. Detach the training input volume from its staging node before attaching it to a different GPU node.

Archive the approved acquisition, the first and final receipts, and the exact `inventory.json` bytes in the protected operator records. Both cleanup hosts and both worker scopes use those same bytes and their `baseline_sha256`. The only initial disk IDs are the three acquired volumes. GPU entry additionally allows the recorded support worker boot disk, instance and group by their scope fields. On release verify zero run workers and boot disks, no backend lock, and exact provider equality to all three intended retained disks with no stale Kubernetes or provider attachment. The generic release check detects extras. The independent exact comparison also detects a missing retained disk. Hand the verified identifiers, actual billing and agreed retention date to the retention owner. [Guarded worker allocation](../../bootstrap/nebius/management/README.md#check-the-allocation)
