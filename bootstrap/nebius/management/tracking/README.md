# Private experiment tracking on the support worker

The support worker hosts one PostgreSQL instance and one MLflow pod. MLflow binds loopback and Caddy exposes HTTPS inside the cluster. The signed trainer receives the service CA and a bearer token. PostgreSQL uses a separate application role with verified TLS. Only the MLflow container receives the scoped S3 writer. The two pods request 800m CPU and 1,600 MiB in total. Include the 16 GiB retained database disk, artifact versions, backup storage, and the CPU worker through final export in the experiment ledger.

### Prepare the operator-owned objects

Work from the reviewed deployment commit on bm. Verify the signed image release and the cluster endpoint before issuing credentials. In Bash, from the repository root:

```bash
source ~/.config/ml-infra/nebius-k8s-public.env
k=(kubectl --kubeconfig "$HOME/.kube/nebius-prod" --context prod-nebius)
python3 workloads/sky/private_api.py "${k[@]}"
"${k[@]}" auth whoami
stack=bootstrap/nebius/management/tracking
# Use persistent management storage with an owner-only parent.
work=$(mktemp -d "$private_parent/tracking-window-XXXXXXXX")
kubectl kustomize "$stack" > "$work/stack.yaml"
```

Create the Restricted namespace and its two token-free service accounts, then validate the complete stack against the managed API. Review the resulting objects before starting a funded window. Use the existing bootstrap field manager for the held admission policy. The native submitter stays closed during support qualification.

```bash
"${k[@]}" -n tracking apply --server-side --field-manager=nebius-tracking-operator -f "$stack/namespace.yaml"
"${k[@]}" apply --server-side --dry-run=server --validate=strict --field-manager=nebius-tracking-operator -f "$work/stack.yaml"
"${k[@]}" apply --server-side --dry-run=server --validate=strict --field-manager=nebius-tracking-operator -f "$stack/client-network.yaml"
```

Use an owner-only directory on persistent management storage. Run `issue-credentials` once for a new deployment and retain the resulting directory for recovery. Reuse its path when retrying. It contains the issuing CA key, two distinct server keys, two database passwords, and a bearer token. Verify the configured kubeconfig before applying the following file-backed objects. The scope of the S3 writer must be the approved bucket's artifact prefix. Verify denial for delete and unrelated storage, and record the credential expiry against the planned window.

```bash
certs=$("$stack/issue-credentials" "$private_parent")
store_keys="$private_writer_credentials_directory"
"${k[@]}" -n tracking create secret generic mlflow-proxy \
  --from-file=tls.crt="$certs/mlflow.crt" --from-file=tls.key="$certs/mlflow.key" --from-file=bearer="$certs/bearer"
"${k[@]}" -n tracking create secret generic mlflow-postgres-tls \
  --from-file=tls.crt="$certs/postgres.crt" --from-file=tls.key="$certs/postgres.key" --from-file=ca.crt="$certs/ca.crt"
"${k[@]}" -n tracking create secret generic mlflow-postgres \
  --from-file=password="$certs/password" --from-file=admin-password="$certs/admin-password"
"${k[@]}" -n tracking create secret generic mlflow-artifacts \
  --from-file=access-key-id="$store_keys/access-key-id" --from-file=secret-access-key="$store_keys/secret-access-key"
"${k[@]}" -n tracking create configmap mlflow-artifact-target --from-literal=destination="s3://$tracking_bucket/artifacts"
"${k[@]}" -n training create configmap episode-tracking \
  --from-literal=https-uri=https://mlflow.tracking.svc.cluster.local --from-file=ca.crt="$certs/ca.crt"
"${k[@]}" -n training create secret generic episode-tracking --from-file=token="$certs/bearer"
```

On an interrupted installation, compare existing named objects against the original private files locally and create the missing objects. Keep Secret bytes out of terminal output and Git. Issue new application passwords against the live PostgreSQL role when rotating. Restart the affected clients and servers to reload file- or environment-backed credentials. Keep the CA signing key on the management host.

### Start and qualify

Confirm the eu-south1 disk count covers both boot disks and all three retained claims. Record the retained disks and their ownership in the reviewed funding and cleanup inventory. Follow the guarded support allocation and record the provider ID of the Ready CPU node. Apply `client-network.yaml` and the held `episode-submit-inputs` policy with their reviewed managers. Inspect the existing additive policies, the live managed CoreDNS selector, Cilium DNS enforcement, and workload namespace bindings. Verify the resulting TLS and storage routes from the actual Pods.

```bash
"${k[@]}" apply --server-side --field-manager=nebius-tracking-operator -f "$stack/client-network.yaml"
"${k[@]}" apply --server-side --field-manager=nebius-tracking-operator -f "$work/stack.yaml"
"${k[@]}" -n tracking rollout status statefulset/postgres --timeout=5m
"${k[@]}" -n tracking rollout status deployment/mlflow --timeout=6m
```

Using the scoped qualification identity, run the [support worker tracking check](../../README.md#qualify-the-tracking-service-on-the-support-and-gpu-workers). Save its authenticated metric and artifact read-back, both credential-denial checks and the worker identity. Also run the actual CPU image pull and staging preflight, verify a trainer cannot reach the storage endpoint directly, and verify the stager can reach only the versioned storage route and DNS. Clear pending Jobs and record any failure before funding a GPU. Repeat the tracking check from the funded GPU worker before the first update.

### Back up, restore and retire

Quiesce new fits and finish pending tracking writes. Record the original tracking probe receipt. Scale MLflow to zero and wait for its pod to terminate before pairing a PostgreSQL dump with an artifact snapshot. Use the same protected operator directory for the dump and its receipt. All S3 backup operations use separate operator credentials restricted to the source or restore destination. Versioned object keys and database contents stay private.

```bash
umask 077
"${k[@]}" -n tracking scale deployment/mlflow --replicas=0
"${k[@]}" -n tracking wait --for=delete pod -l app=mlflow --timeout=150s
"${k[@]}" -n tracking exec postgres-0 -- pg_dump -U postgres -Fc --no-acl --dbname=mlflow > "$work/metadata.dump"
"${k[@]}" -n tracking exec -i postgres-0 -- pg_restore --list < "$work/metadata.dump" > "$work/dump-catalog.txt"
python3 "$stack/backup-artifacts.py" snapshot --bucket "$tracking_bucket" --prefix artifacts/ \
  --directory "$work/artifacts" --max-bytes 1073741824 --max-objects 8192 > "$work/artifact-receipt.json"
(cd "$work" && sha256sum metadata.dump artifacts/manifest.json > backup.sha256)
```

Copy the backup and digests to the approved private recovery location. Follow the [restore and cutback procedure](recovery.md#rehearse-recovery-on-the-existing-database-disk) to verify the same run against fresh database and artifact stores and again after returning to the original. Use the [management reporting command](recovery.md#read-the-private-tracking-service-from-bm) to export final curves before releasing the support worker.

At final close, retire the service and its credentials, record the retained PV and exact artifact versions with their owners and expiry, and reconcile their cost through deletion. The worker cutoff controls continue throughout.
