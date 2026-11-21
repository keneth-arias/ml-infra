# Management reporting and recovery

Use the reviewed deployment checkout and the same independently pinned management kubeconfig as the [tracking runbook](README.md). Keep credentials, backup files, and receipts in an owner-only directory. Run management reads before releasing the support worker or during a separately authorized CPU support window.

## Read the private tracking service from bm

Run from the repository root. Use the original deployment's public CA and bearer files from `issue-credentials`. The runner opens a temporary IPv4 loopback port-forward to the named HTTPS port of `service/mlflow` in `tracking`. Its Python process resolves only the fully qualified Service name and assigned port to that forward. The URL keeps the canonical DNS name for TLS SNI, hostname verification and the HTTP Host header. Caddy forwards the canonical hostname to MLflow. The runner checks that missing and incorrect credentials get HTTP 401 and the supplied credential gets HTTP 200 over verified TLS. It closes the forward on exit and has a three-minute deadline. The CA and bearer are read from files, and the runner bypasses inherited network proxies.

```bash
umask 077
source ~/.config/ml-infra/nebius-k8s-public.env
export NEBIUS_API_ENDPOINT=public
stack=bootstrap/nebius/management/tracking
operator_kubeconfig="$HOME/.kube/nebius-prod"
uv run "$stack/through-forward.py" --operator-kubeconfig "$operator_kubeconfig" \
  --ca "$certs/ca.crt" --bearer "$certs/bearer" report \
  --run "$control_run" --run "$candidate_run" --output "$work/final-curves"
```

Supply both completed arms of each seed. The report command verifies the recorded result, evaluation history and checkpoint manifest before publishing the new report directory. A failed or interrupted command can be retried once the private Service is healthy and the output path is confirmed absent. The same runner can verify a known synthetic receipt using `restore-check`.

## Rehearse recovery on the existing database disk

Use a quiesced support-only interval with enough time remaining for recovery and cutback. Hold training and staging submissions, stop and reconcile pending clients, and keep the training-side Secret and ConfigMap pointed at the original deployment. Use the paired dump and versioned artifact snapshot made after stopping the original MLflow, and verify the hashes against the independently retained receipt. Record the live PVC, PV and cloud disk IDs. Use the existing 16 GiB retained PVC and a separate logical database on it. Confirm available filesystem space covers the restored tables and indexes, temporary files and WAL alongside the original database, using the backup size and measured restore footprint. If space or funded time is insufficient, leave the original intact and defer the rehearsal to an approved window. The rehearsal creates no PVC, disk or worker.

Prepare a distinct empty, versioned artifact bucket, funded through its cleanup date. Use the restore operator credential for object copying. Use a fresh scoped MLflow credential for the restored artifact prefix. Verify the original writer cannot access the restore bucket and the restore writer cannot access the original bucket. Save proof of both denials. The restored MLflow uses a different bearer, and the ordinary training clients remain closed throughout this exercise.

```bash
set -euo pipefail
umask 077
source ~/.config/ml-infra/nebius-k8s-public.env
export NEBIUS_API_ENDPOINT=public
k=(kubectl --kubeconfig "$HOME/.kube/nebius-prod" --context prod-nebius)
python3 workloads/sky/private_api.py "${k[@]}"
"${k[@]}" auth whoami
kt=("${k[@]}" -n tracking)
stack=bootstrap/nebius/management/tracking
(cd "$work" && sha256sum --check backup.sha256)

rev="recovery-$(date -u +%Y%m%d%H%M%S)"
recovery=$(mktemp -d "$private_parent/$rev.XXXXXXXX")
"${kt[@]}" get deployment mlflow -o json |
  jq -e '.spec.template | select(any(.spec.containers[] | select(.name == "mlflow") | .env[];
    .name == "AWS_ACCESS_KEY_ID" and .valueFrom.secretKeyRef.name == "mlflow-artifacts")
    and any(.spec.volumes[]; .name == "db-password" and .secret.secretName == "mlflow-postgres"))' \
  > "$recovery/original.template.json"

quiesce_mlflow() {
  "${kt[@]}" scale deployment/mlflow --replicas=0
  local pods pod
  pods=$("${kt[@]}" get pods -l app=mlflow -o name)
  if [[ -n $pods ]]; then
    while IFS= read -r pod; do
      "${kt[@]}" wait --for=delete "$pod" --timeout=150s
    done <<< "$pods"
  fi
  test -z "$("${kt[@]}" get pods -l app=mlflow -o name)"
}
quiesce_mlflow
"${kt[@]}" exec postgres-0 -- df -B1 /var/lib/postgresql/data
"${kt[@]}" exec postgres-0 -- psql -X -U postgres -d postgres -Atqc \
  "SELECT pg_database_size('mlflow')"
```

Use the reviewed stack that includes the separate TLS/SCRAM HBA entry for `mlflow_restore`. If updating a running installation, reconcile the reviewed runtime ConfigMap with MLflow at zero replicas and restart PostgreSQL on the same PVC before this step. Verify PostgreSQL is Ready, `pg_hba_file_rules` reports no errors and shows both distinct hostssl role/database pairs, and the original database's pre-restore receipt still exists.

Confirm both the `mlflow_restore` role and database are absent. If either already exists, reconcile its ownership and evidence from the interrupted rehearsal before proceeding. The database name is deliberately fixed for the restore-mode launcher, and no part of the procedure drops or overwrites the original `mlflow` database.

```bash
"${kt[@]}" exec postgres-0 -- psql -X -U postgres -d postgres -Atqc \
  "SELECT rolname FROM pg_roles WHERE rolname='mlflow_restore';
   SELECT datname FROM pg_database WHERE datname='mlflow_restore';"

# Proceed only after the preceding command returned no rows.
"${kt[@]}" exec -i postgres-0 -- psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE mlflow_restore LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE mlflow_restore OWNER mlflow_restore;
REVOKE CONNECT ON DATABASE mlflow_restore FROM PUBLIC;
SQL
openssl rand -hex 32 | tr -d '\n' > "$recovery/password"
{
  cat "$recovery/password"; printf '\n'
  cat "$recovery/password"; printf '\n'
} | "${kt[@]}" exec -i postgres-0 -- psql -X --no-password -U postgres -d postgres \
  -v ON_ERROR_STOP=1 -c '\password mlflow_restore'
"${kt[@]}" exec -i postgres-0 -- pg_restore -U postgres --dbname=mlflow_restore \
  --role=mlflow_restore --no-owner --no-acl --exit-on-error --single-transaction \
  < "$work/metadata.dump"
"${kt[@]}" exec postgres-0 -- psql -X -U postgres -d postgres -Atqc \
  "SELECT NOT has_database_privilege('mlflow','mlflow_restore','CONNECT')"
```

Require `t` from the privilege check. Use the separate restore operator's file-backed AWS profile for the copy. Ensure ambient AWS environment variables cannot select the original writer. Complete the destination verification before starting the restored MLflow.

```bash
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
  AWS_SHARED_CREDENTIALS_FILE="$restore_operator_credentials_file" AWS_PROFILE="$restore_operator_profile" \
  python3 "$stack/backup-artifacts.py" restore --bucket "$restore_bucket" --prefix artifacts/ \
    --directory "$work/artifacts" --max-bytes 1073741824 --max-objects 8192 \
  > "$recovery/artifact-restore.receipt.json"
```

### Switch the idle service using new credential revisions

The restored pod uses the original TLS leaf and trust path for the same Service name, with a distinct bearer and distinct database and object-store credentials. Leave the original Secrets and artifact ConfigMap untouched. The following commands use the restore writer's owner-only files.

```bash
openssl rand -base64 32 | tr '+/' '_-' | tr -d '=\n' > "$recovery/bearer"
"${kt[@]}" create secret generic "$rev-db" --from-file=password="$recovery/password"
"${kt[@]}" create secret generic "$rev-proxy" \
  --from-file=tls.crt="$certs/mlflow.crt" --from-file=tls.key="$certs/mlflow.key" \
  --from-file=bearer="$recovery/bearer"
"${kt[@]}" create secret generic "$rev-artifacts" \
  --from-file=access-key-id="$restore_writer_keys/access-key-id" \
  --from-file=secret-access-key="$restore_writer_keys/secret-access-key"
"${kt[@]}" create configmap "$rev-target" --from-literal=destination="s3://$restore_bucket/artifacts"

jq --arg rev "$rev" '
  (.spec.containers[] | select(.name == "mlflow") | .env) |=
    (map(if .name == "MLFLOW_ARTIFACTS_DESTINATION" then
       .valueFrom.configMapKeyRef.name = ($rev + "-target")
     elif .name == "AWS_ACCESS_KEY_ID" or .name == "AWS_SECRET_ACCESS_KEY" then
       .valueFrom.secretKeyRef.name = ($rev + "-artifacts")
     else . end) + [{name: "MLFLOW_RESTORE_DB", value: "true"}])
  | .spec.volumes |= map(
      if .name == "db-password" then .secret.secretName = ($rev + "-db")
      elif .name == "proxy-secret" then .secret.secretName = ($rev + "-proxy")
      else . end)
' "$recovery/original.template.json" > "$recovery/restore.template.json"

switch_template() {
  local before=$1 after=$2 saved=$3 version
  version=$("${kt[@]}" get deployment mlflow -o jsonpath='{.metadata.resourceVersion}')
  jq -n --arg version "$version" --slurpfile before "$before" --slurpfile after "$after" '
    [{op: "test", path: "/metadata/resourceVersion", value: $version},
     {op: "test", path: "/spec/replicas", value: 0},
     {op: "test", path: "/spec/template", value: $before[0]},
     {op: "replace", path: "/spec/template", value: $after[0]}]
  ' > "$recovery/switch.patch.json"
  "${kt[@]}" patch deployment mlflow --type=json --patch-file="$recovery/switch.patch.json" -o json |
    jq -e '.spec.template' > "$saved"
}
switch_template "$recovery/original.template.json" "$recovery/restore.template.json" \
  "$recovery/restore.live.template.json"
"${kt[@]}" scale deployment/mlflow --replicas=1
"${kt[@]}" rollout status deployment/mlflow --timeout=6m
uv run "$stack/through-forward.py" --operator-kubeconfig "$HOME/.kube/nebius-prod" \
  --ca "$certs/ca.crt" --bearer "$recovery/bearer" restore-check \
  --read-receipt "$original_inner_tracking_receipt" > "$recovery/restore.receipt.json"
```

Require matching run ID, experiment ID, metric and downloaded artifact digest, and the credential denials in the resulting receipt. The check only reads the restored stores. A connection failure or unexpected template is a failed rehearsal. If a patch result is uncertain, read the live template and compare it to the saved original and restore templates before taking the next action.

### Cut back to the original stores

Perform cutback even when restore verification fails. Scale the restored deployment to zero and wait for all its pods to terminate. Switch back only if the live template still matches the recorded restore template. If the first switch failed and the live template still matches the recorded original, leave that template in place. On any unexpected template, leave MLflow at zero and reconcile the differences before proceeding.

```bash
quiesce_mlflow
switch_template "$recovery/restore.live.template.json" "$recovery/original.template.json" \
  "$recovery/cutback.live.template.json"
jq -e --slurpfile live "$recovery/cutback.live.template.json"   '. == $live[0]' "$recovery/original.template.json"
```

If the support window is still authorized and the experiment continues, scale MLflow to one, wait for its rollout, and re-run the same readback with the **original bearer** and receipt. Confirm the original PV, database, artifact bucket and writer references are in service. Reopen original clients only after that check passes. When retiring instead, leave MLflow quiescent and follow the worker cutoff and retained storage procedure.

After verified cutback, revoke the restore credentials and record the restore database, Secrets, bucket versions and receipts with their owner and expiry. Remove the restored stores only under their reviewed cleanup plan. Keep the original DB, original artifact versions, and independently verified backup through the approved retention period.
