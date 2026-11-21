# fw13 backup cleanup

The backup runs in the `nebius-cleanup` namespace on fw13’s Bazzite kind cluster, context `kind-kind-cluster`. The primary remains the bm systemd timer. Only the funding host can reserve an allocation. A rehearsal Pod cannot write a funding receipt or delete a cloud resource.

## Stage the backup

1. On the build host, prepare a tools directory containing the reviewed linux/amd64 `terraform` and `nebius` binaries and `mirror/registry.terraform.io/nebius/nebius/0.6.88/linux_amd64/` from the provider install. Keep credentials outside this directory. The build checks the binaries against their pinned hashes and installs the mirror against the committed provider lock. It runs Terraform validation as UID 1000. Record the source commit and OCI manifest digest and retain the archive outside fw13.
2. Import the OCI bytes into each eligible kind node. For the single-node fw13 installation the following commands address containerd in the named node. The scheduler uses the local digest with pull policy `Never`. A rebuilt node needs another import before arming.

```bash
kind_dir=bootstrap/nebius/management/kind
sha=$(git rev-parse --verify HEAD)
"$kind_dir/build" "$sha" "$tools" "$out"
image="docker.io/local/nebius-cleanup@$(jq -er '.["containerimage.digest"]' "$out/build.json")"
ssh fw13 'docker -H unix:///var/run/docker.sock exec -i kind-cluster-control-plane ctr -n k8s.io images import --all-platforms --digests -' < "$out/worker.tar"
ssh -n fw13 "docker -H unix:///var/run/docker.sock exec kind-cluster-control-plane ctr -n k8s.io images tag docker.io/local/nebius-cleanup:$sha $image"
python3 "$kind_dir/render.py" "$run" "$stage" "$image" > "$out/suspended.json"
ssh fw13 'kubectl --context=kind-kind-cluster apply --server-side --dry-run=server --field-manager=nebius-cleanup-bootstrap -f -' < "$out/suspended.json"
ssh fw13 'kubectl --context=kind-kind-cluster apply --server-side --field-manager=nebius-cleanup-bootstrap -f -' < "$out/suspended.json"
```

3. With a separate disposable run ID, apply the same renderer with `--armed --rehearsal`. Wait for a controller-scheduled Job to complete and retain its JSON log. It verifies the Pod → Job → CronJob ownership chain and TLS to the cloud API and S3. Suspend through a missed minute, resume, and verify a new Job and Pod UID. Delete the disposable CronJob after recording the evidence. Confirm the ServiceAccount can get scheduler objects and cannot read Secrets. Verify host and kind recovery after a planned restart before relying on unattended takeover. If the host, node or control plane is down, the backup can act when scheduling resumes. bm remains independent of fw13.

## Arm a funded window

Use one CronJob per run. The operator owns its template, trust roots, grants and stage configuration. Only administrators trusted with cloud cleanup credentials may change workloads in this namespace. The Pod gets a short-lived projected service-account token with get-only access to scheduler objects. Every tick has a private scratch directory and no host mounts.

| Namespace object | Contents |
| --- | --- |
| Secret `fw13-auth-key` | `auth.pem` for the independent fw13 cleanup service account. Register its public half. |
| Secret `expiry-RUN-state` | `aws-credentials` in AWS default-profile format with access to the exact backend, lock and fw13 proof location. |
| Secret `fw13-otlp` | `otlp-bearer` for the approved VictoriaLogs ingestion endpoint. Mount the LAN endpoint’s trusted Caddy root from ConfigMap `fw13-otlp-ca` as `ca.crt`. Source the public root from the operator trust store. |
| ConfigMap `expiry-RUN-STAGE` | `scope.json`, `inventory.json` and the complete reviewed non-secret `terraform.tfvars` for that stage. |

The fw13 LAN proxy permits the observed kind node peer to POST to the single VictoriaLogs path on `192.168.50.155:8443`. The proxy matches the immediate peer’s /32, listener address, method, exact path, empty query and bearer scheme. Victoria’s ingestion gateway authenticates the bearer. If the node address changes, verify the new peer before updating that rule. Use a synthetic run to verify loss of credentials, a different method and a different path are denied.

Create these objects from protected files and keep credentials outside the Terraform variables and Git. Make the scope ConfigMap immutable. The pilot and subsequent GPU window have distinct run IDs. Support and GPU stages share the GPU window’s cutoff. Stage a new immutable ConfigMap before changing that run’s template and keep bm armed through the switch.

1. Render with `--armed` and obtain the defaulted CronJob from **server-side dry run**. Record the existing CronJob UID, the digest-pinned image and the SHA-256 of its complete armed spec in `scope.fw13_cronjob` under `uid`, `image` and `spec_sha256`. Use `worker_cleanup.scope_digest(spec)` on the returned JSON. The suspended spec has a different digest. Bind `fw13_tfvars_sha256` to the exact variables file bytes, `fw13_public_key_id` to the enrolled key and `fw13_attest_until` to the UTC end of the brief pre-allocation proof window. The same approved scope goes to bm. Record independent, versioned grant and budget evidence as specified in the [allocation runbook](../README.md).
2. The kind worker uses `https://192.168.50.155:8443/insert/opentelemetry/v1/logs` and verifies the IP address against the server certificate. Before publishing allocation evidence it sends an authenticated, empty OTLP batch through the same route. An ingress or credential denial blocks the proof. This check emits zero log records. Alert delivery errors do not defer cutoff cleanup. Exercise the mounted notifier with a synthetic run ID and `CLEANUP_ALERT_REHEARSAL=1`. Verify the event ID in VictoriaLogs and the matching Grafana rule. Pod-side alerts cover running jobs. From bm check fw13 scheduler, node and Job health as well: a stopped cluster or a failed image/Secret mount cannot send its own failure.
3. Apply the reviewed armed spec. Before the cutoff the worker rehearses a locked, ownership-checked release plan and compares inventory with its approved baseline. During the proof window the scheduled worker writes a versioned `prod/mk8s/release-control/timers/fw13/RUN/STAGE.json` in the approved control bucket in the management project. The prefix grant for fw13 ends at `timers/fw13/`. Match its CronJob UID, spec, image, scheduled minute, Job/Pod UIDs, scope and live state serial. An old systemd receipt, a manual Job, or a late proof blocks funding. Reconcile the fresh bm proof before consuming the plan.
   If an allocation finishes during this proof window, a subsequent tick checks the locked release plan against a fresh provider snapshot and confirms the expected newly allocated group, running VM and ready boot disk attached read-write to that VM. Additional attached disks must already appear in the approved inventory. A verified allocation does not produce another funding proof. An incomplete allocation, unexpected resources or disagreement between state and provider inventory still triggers an alert. The cutoff branch remains independent of the proof window.
4. At UTC cutoff the backup initializes the approved backend and calls the same `expire-workers --apply` used on bm. The proof deadline and control bucket apply to allocation. Cutoff cleanup depends on the Terraform backend and its lock in the state bucket. Jobs run every minute with overlap forbidden. Failed or interrupted releases retry on later ticks. Keep both schedulers active until their inventory matches the approved shared resources and retention decisions. Capture the actual deletion, final IDs and bill before suspending and retiring the run. Preserve the image and evidence through close-out.

When the ten-minute worker deadline expires or Kubernetes sends SIGTERM, the supervisor fences new Terraform commands, sends a single interrupt to the active Terraform CLI and waits up to 75 seconds for state persistence and unlock. It then reports the failed tick. A hard node loss or a provider that outlasts this grace can leave an S3 lock. Before unlocking, inspect the backend lock ID, both schedulers’ Jobs or service invocations, Nebius operations and current inventory. Quiesce competing runs, confirm the lock owner has stopped and no provider operation is outstanding, then have the state owner release that exact stale lock. Re-arm both schedulers and reconcile the guarded release. Never delete a live lock to accelerate takeover.
