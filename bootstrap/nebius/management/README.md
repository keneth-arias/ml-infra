# Worker allocation and cleanup


## Install the attended SkyPilot backend on bm

The production client connects through an authenticated HTTPS proxy. SkyPilot 0.14.3 listens on `127.0.0.1:46581` under the dedicated `sky-cpu` account. Caddy 2.11.4 runs as `sky-cpu-proxy` and serves `https://sky-cpu.localhost:46582` on IPv4 loopback. The two identities can reach the raw port through the host's UID-based firewall. Named proxy users share the backend's scoped `sky-cpu:sky-backend` permissions.

Use a reviewed revision of this repository for the installation. Keep the two service accounts, private `/var/lib/sky-cpu` state, the host's TLS key, the operator password hashes and the session kubeconfig on the management host. Create the accounts before checking the firewall table because nftables resolves their names. Install these components as root-owned files, readable by their respective services.

| Destination | Reviewed inputs |
| --- | --- |
| `/opt/ml-infra` | Preserve the repository paths for `workloads/sky/{private_api,production_target}.py`, `bootstrap/nebius/management/{check-backend.py,config.yaml}`, and `management/rootless/{install-template.py,build-wheels.txt}` under the same bootstrap directory. |
| `/etc/sky-cpu` | [Server configuration](config.yaml), [Caddyfile](Caddyfile), [nftables table](sky-cpu.nft). Set `SKY_CPU_CERT` and `SKY_CPU_KEY` in `proxy.env`. Limit access to the TLS key and `operators.caddy` (Caddy basic-auth username and password hash entries) to the proxy. |
| `/etc/systemd/system` | [Backend service](sky-cpu.service), [TLS proxy service](sky-cpu-proxy.service) and [firewall service](sky-cpu-firewall.service). |
| `/opt/sky-cpu` | Dedicated CPython 3.11 environment with the [locked rootless SkyPilot package and adapter](rootless/README.md). Install Caddy at `/opt/sky-cpu-proxy/bin/caddy` and provide `kubectl`, OpenSSH, rsync, socat and nc on the unit's PATH. |

Download the three build wheels from the reviewed lock with `python3.11 -m pip download --only-binary=:all: --no-deps --require-hashes -r bootstrap/nebius/management/rootless/build-wheels.txt -d "$wheel_stage"` and install them in the dedicated `/opt/sky-cpu-build-wheels` directory. The backend checks the installed adapter, package version, wheel names and hashes, namespace, only allowed context, pinned managed API CA, direct endpoint and authenticated ServiceAccount before binding its port.

bm's earlier 0.14.3 environment contained all 127 locked versions and an older adaptation of `kubernetes-ray.yml.j2`. The current start check rejected it before binding. On an upgrade, stop both services and save the old adapter and adapted files. For this known predecessor, the old adapter's `--check` authenticated both installed components and the stock template backup matched the current adapter's `STOCK` SHA-256. Restoring that verified stock template in the idle environment allowed the released installer to apply its changes. Run the released adapter's `--check` as the service user. Recreate an environment with unrecognized modifications from the lock before starting the service.

On bm, append the following exact directive at the **end** of the host's existing `/etc/nftables.conf` after its ruleset reset. Use the host's configuration owner to preserve this include during upgrades. The include reinstalls the pilot table in the same atomic transaction as a host ruleset reload. Validate with `sudo nft -c -f /etc/nftables.conf`. The pilot firewall unit checks for the include and loads only its dedicated table. It follows nftables service stops, and the backend follows the pilot firewall. bm uses UFW and keeps the standalone nftables service disabled. Keep that host setting and rehearse full ruleset reloads in a disposable network namespace.

```nft
include "/etc/sky-cpu/sky-cpu.nft"
```

In `/etc/sky-cpu/target.env`, set the independent cluster ID, CA digest and approved endpoint mode. Run `systemd-analyze verify` on the installed units and `systemctl daemon-reload`. Leave the application units disabled at boot. Use the bounded marker-backed credential issued below; the API identity check verifies the account. The issuer, session expiry and cutoff timer bound the attended session. Deliver its backend kubeconfig with mode 0600 owned by `sky-cpu`. After issuance and before starting services, arm a stop using the earliest session expiry. Use a three-minute margin for bm's observed 90-second backend stop. The command expects the protected issuance receipts from this session.

```bash
set -euo pipefail
session_receipts=("$sessions/backend-a/issued.json" "$sessions/native/issued.json")
earliest=$(for record in "${session_receipts[@]}"; do
  date -u -d "$(jq -er '.expires_at' "$record")" +%s || exit 1
done | sort -n | head -1)
stop_epoch=$((earliest - 180))
(( stop_epoch > $(date -u +%s) ))
at=$(date -u -d "@$stop_epoch" '+%Y-%m-%d %H:%M:%S UTC')
unit="sky-cpu-close-$(date -u +%Y%m%d%H%M%S)"
sudo systemd-run --unit="$unit" --on-calendar="$at" --timer-property=AccuracySec=1s \
  /usr/bin/systemctl stop sky-cpu-proxy.service sky-cpu.service
systemctl is-active "$unit.timer"
systemctl show "$unit.timer" -p NextElapseUSecRealtime
sudo systemctl start sky-cpu-proxy.service
```

Starting the proxy pulls in the protected backend and firewall. After renewing during an idle handoff, restart the backend, start the proxy explicitly, and verify the authenticated client reports the expected healthy endpoint and versions. To replace the timer after turnover, set `session_receipts` to every session that will still be active (for example `backend-b/issued.json` and the current native receipt). Keep the earlier stop armed until the replacement has a verified firing time within those expiries and the superseded credential has been revoked. Check an anonymous TLS request receives 401. Submit only after independent admission, funding and worker checks. At close, stop both application services, revoke each issued session and remove delivered kubeconfig copies as described below.

From the bm operator account set `NEBIUS_CPU_SKY_URL=https://sky-cpu.localhost:46582` and `SKYPILOT_API_SERVER_ENDPOINT="$NEBIUS_CPU_SKY_URL"`. The management TLS certificate must cover the DNS name `sky-cpu.localhost`. Set `REQUESTS_CA_BUNDLE` to the independently trusted proxy CA and `NETRC` to the operator-owned mode-0600 netrc entry for that hostname. Include the hostname and loopback in `NO_PROXY`. Run `/opt/sky-cpu/bin/sky api info -o json` and verify both versions are 0.14.3, the returned URL matches and the server is healthy. Keep the netrc, keys and session files outside the repository.

## Rotate credentials for an attended CPU session

The operator on bm can issue a bounded credential for `sky-cpu:sky-backend` or `sky-cpu:cpu-submit` with [cpu-session.py](cpu-session.py). Each session creates an immutable, empty Secret in `sky-cpu`. Kubernetes binds the requested API token to that marker's UID. Revoking one session leaves the ServiceAccount and its other sessions available. The expiry and API audiences in `issued.json` are the values returned by the managed API. [Kubernetes bound service account tokens](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/#bound-service-account-tokens)

On the trusted operator host, use the production cluster ID and CA digest recorded independently from the Nebius management API. Select the operator kubeconfig for the approved public endpoint on bm. A kubeconfig generated with `--internal` belongs to the private route. For that route select `NEBIUS_API_ENDPOINT=private` throughout instead. Keep the sessions in a private, operator-owned directory on temporary storage. The protected `kubeconfig` is the only file for delivery to its intended consumer. The marker and issuance receipts can be retained separately for audit.

```bash
umask 077
sessions=$(mktemp -d "${XDG_RUNTIME_DIR:?}/cpu-pilot.XXXXXXXX")
export NEBIUS_API_ENDPOINT=public
: "${OPERATOR_CONFIG:?select the verified operator kubeconfig for this endpoint}"
: "${NEBIUS_TRAINING_CLUSTER_ID:?}" "${NEBIUS_CA_BUNDLE_SHA256:?}"
export NEBIUS_TRAINING_CLUSTER_ID NEBIUS_CA_BUNDLE_SHA256
issuer=(python3 bootstrap/nebius/management/cpu-session.py
  --operator-config "$OPERATOR_CONFIG" --endpoint "$NEBIUS_API_ENDPOINT"
  --cluster-id "$NEBIUS_TRAINING_CLUSTER_ID" --ca-bundle-sha256 "$NEBIUS_CA_BUNDLE_SHA256")
"${issuer[@]}" issue --account sky-backend --seconds 1800 "$sessions/backend-a"
"${issuer[@]}" issue --account cpu-submit --seconds 1800 "$sessions/native"
```

Install the backend kubeconfig atomically with owner `sky-cpu` and mode 0600. Give the native configuration to the native client. Set an attended stop timer before starting the authenticated HTTPS proxy and isolated SkyPilot API. Allow for backend shutdown and restart before the earliest credential expiry. For renewal, issue a second session for the same account into a new directory, atomically replace the delivered file, and restart the backend during an idle handoff. Verify the effective Kubernetes identity, namespace, authenticated client/server version and scoped access with the new credential. Run the [CPU admission probe](../../../workloads/smoke/check-cpu-admission.py) with the endpoint, cluster ID and CA pin from the issuance environment and the new backend credential. For example, from the checkout root run `NEBIUS_SUBMIT_KUBECONFIG="$sessions/backend-b/kubeconfig" uv run workloads/smoke/check-cpu-admission.py "$ROOTLESS_CPU_IMAGE"` with the independently verified image reference. Scheduling and image pulls require a funded worker.

Revoke the old session with `"${issuer[@]}" revoke "$sessions/backend-a"`. The delete is conditional on the original marker UID. When the issued kubeconfig is available the command waits up to 75 seconds for its own SelfSubjectReview to return HTTP 401 before removing it. Verify the new session still works. If a request times out or the API returns an inconclusive status, retain the session directory and retry closeout. At closure stop the attended services, revoke each remaining session, remove delivered credential copies and capture the inventory. If the issue process lost the marker-create response, `intent.json` identifies the marker to reconcile with the operator context. Verify its name, session label, UID and immutable empty contents before removing that orphan. A run interrupted after `marker.json` was written can be closed directly with `revoke`. An unattended token broker remains a separate deployment.

## Qualify the native CPU Job

Use the private `cpu-submit` kubeconfig from the attended session. The [native pilot](../../../workloads/smoke/native-cpu-pilot.py) checks the managed API endpoint, CA, context, namespace and effective submitter before contacting the Jobs API. Its default operation is a strict server-side admission dry run. Before funding, a placeholder node name exercises admission with zero scheduled pods.

```bash
export NEBIUS_SUBMIT_KUBECONFIG="$sessions/native/kubeconfig"
python3 workloads/smoke/native-cpu-pilot.py admission-check --node cpu-pilot-placeholder
```

During the funded CPU window, use the operator inventory to match the private Compute instance and its Ready Kubernetes node, confirm the CPU pool label, zero GPU groups and Ready managed CoreDNS endpoints, and record the node name in `CPU_NODE`. Use a unique run name and capture the JSON receipt outside the worker. The Job requires that exact node through the default scheduler, a single CPU and 2 GiB, and has a three-minute deadline.

```bash
python3 workloads/smoke/native-cpu-pilot.py "$run_id" --node "$CPU_NODE" --execute > "$receipts/native-job.json"
```

A successful receipt binds the returned Job and Pod UIDs to the node, the admitted image reference, the container runtime's image ID, and the in-pod checks. The probe checks that cluster DNS returns the Kubernetes Service IP and that a direct TCP connection to that IP on port 443 is blocked. It also checks UID 65532, an empty effective capability set, seccomp filtering, disabled privilege escalation and absence of a service account token. The runner deletes its Job using a UID precondition and checks that its Pods have gone before emitting success. If a create response is lost, inspect the named Job and its owner-linked Pods before retrying. A finished orphan Job expires after fifteen minutes.

The native probe uses the admitted public Python image. Qualify the separate signed rootless image through the [SkyPilot worker procedure](rootless/README.md) during the same window and collect its namespace pull, logs, cancellation and teardown evidence. Cloud worker costs continue through node-group release.

## Check the allocation

Keep the reviewed source, backend key, provider lock and off-host ledger under operator control. Write the scope JSON in a private file. The guard expects these keys.

| Key | Input |
|---|---|
| `project_id`, `subnet_id`, `cluster_id`, `run_id` | Independently approved target and unique run |
| `start`, `cutoff` | UTC `Z` timestamps, up to eight hours apart |
| `accrued_gpu_hours`, `remaining_gpu_hours`, `accrued_gpu_usd`, `accrued_non_gpu_usd` | Cumulative ledger across all windows |
| `cpu_and_boot_disk_usd_per_hour` | Fresh quote for one regular CPU worker plus 128 GiB boot SSD |
| `other_remaining_usd`, `cleanup_reserve_usd` | Itemized non-GPU retention, transfer, management, tax and contingency. The guard adds CPU and boot SSD through cutoff plus one hour separately. |

For a read-only CPU topology and budget cross-check, inspect the saved plan from the Terraform root using `./with-credentials ./check-cpu-plan "$plan" "$scope"`. This cross-check uses the non-GPU reserve from the plan. It checks the embedded Terraform source, expected provider defaults, topology, run ownership, plan age and cumulative budget. It prints the SHA-256 of the inspected bytes. The provider leaves some computed fields, including the NIC's public address, unknown before provisioning. The reviewed template requests only the private subnet. Verify actual NICs, disks, node count and effective strategy in the provider inventory at launch. For production allocation use the funded `pilot` stage below to bind the approved scope, provider lock and live state serial. It verifies the exact bytes, checks both timer hosts and atomically reserves the attempt before Terraform can create the group.

**bm runs the primary systemd timer. fw13 runs the backup CronJob in its kind cluster.** Follow the [fw13 deployment and rehearsal](kind/README.md) for its digest, ServiceAccount and scheduler receipt. Keep fw13 powered, awake and connected through the release reserve. Both schedulers use the approved run variables, state backend and inventory. Preserve the pre-run project, cluster, group, instance and disk IDs in `RUN_ID-inventory.json`, including intentionally retained disks. Store newly allocated IDs separately for incident recovery.

On bm install the expiry timer, service and failure-alert unit. Put the pinned executables in `/opt/nebius-cleanup/bin` and the reviewed Terraform root in `/opt/ml-infra/bootstrap/nebius`. Initialize the backend as `nebius-cleanup` with `TF_DATA_DIR=/var/lib/nebius-cleanup/terraform` and `terraform init -lockfile=readonly`. Keep the per-run environment at `/etc/nebius-cleanup/RUN_ID.env` root-owned at 0600 and the inventory readable by the cleanup account at 0640. Arm the bm timer and the fw13 CronJob before provisioning. Retire them after both cloud and Terraform reconciliation. For early release, first remove the run pools from desired configuration and review `expire-workers --release-now` with the same scope and inventory.

For the GPU experiment, close the pilot's inventory and retire its timers. Install and exercise the new bm timer and fw13 CronJob for the GPU window before creating its support CPU or GPU. Record scope, next tick, baseline and alert receipt on each host. After each create, match the actual group IDs and labels from independent cloud reads. Use a new GPU-window run ID after the pilot and fresh stage-specific proofs for the support and GPU creates.

### Cleanup notifications on fw13

On bm, `worker-expiry@.service` triggers [the failure unit](worker-cleanup-alert@.service). Install [notify-cleanup.py](notify-cleanup.py) and the two pure-Python wheels from [the hash lock](otlp-requirements.txt) in the root-owned, service-readable directory `/opt/nebius-cleanup/notifier`. For example, build a staging directory with `uv pip install --target "$stage" --no-deps --only-binary :all: --require-hashes -r otlp-requirements.txt` before installing it. Give the service its origin in `/etc/nebius-cleanup/alert.env` with `CLEANUP_OTLP_LOGS_URL=https://fw13pro.degu-fir.ts.net:8443/insert/opentelemetry/v1/logs`. Install the ingestion bearer at `/etc/nebius-cleanup/otlp-bearer`, root-owned mode 0600. Systemd exposes it to the dynamic UID through `LoadCredential`. Send OTLP protobuf over verified TLS. Retries retain the event ID.

The [Grafana rule](cleanup-alert.grafana.json) in folder `nebius-worker-cleanup` uses datasource `victoria-logs`. It fires per run and host for cleanup failures in the preceding ten minutes. Import its JSON with Grafana’s alert provisioning API after creating that folder. For an acceptance test, set `CLEANUP_ALERT_REHEARSAL=1` on a unique alert-unit instance via a runtime drop-in and induce an invalid-cutoff failure on the matching worker-expiry instance. Check both journals, then query VictoriaLogs with `{service.name="nebius-worker-cleanup"} run_id:="TEST_RUN_ID"`. Match the bm event ID and the fw13 Pod event ID from the kind alert drill in Grafana and VictoriaLogs. The cleanup service has a 12-minute execution timeout and 30-second termination grace. On November 21 a shortened timeout killed a hanging test process on both hosts and triggered the same failure route. A timed-out delete can continue at the provider. Check the operation, lock holder and cloud inventory before retrying or releasing a ledger hold. The November 21 test reached Grafana. Delivery currently ends in the Grafana UI. An off-host paging contact would also cover loss of fw13 itself.

### Saved worker plans and fit decisions

Use [check-gpu-plan](../check-gpu-plan) from the Terraform root for all three stages. Source the provisioning identity environment before running the wrapper. Store approval inputs and receipts outside the worker, under the operator's control. `prepare` initializes the approved eu-south1 S3 bucket, state key and default workspace in a private directory with the reviewed provider lock. It creates a saved plan only after the stage and budget checks pass. `recheck` consumes the trusted prepare receipt and checks the live state lineage and serial, source, current ledger bytes, time and exact plan digest. Both commands leave cloud resources unchanged.

```bash
umask 077
./with-credentials ./check-gpu-plan prepare "$plan" --scope "$scope" --ledger "$ledger" \
  --var-file terraform.tfvars --var-file "$run_vars" > "$prepare_receipt"
./with-credentials ./check-gpu-plan recheck "$plan" --scope "$scope" --ledger "$ledger" \
  --receipt "$prepare_receipt" > "$recheck_receipt"
# For the GPU fit decision after measuring throughput
./check-gpu-plan project --scope "$scope" --ledger "$ledger" \
  --measurements "$timings" > "$fit_receipt"
```

| Input | Fields and semantics |
|---|---|
| Approved scope | `stage` is `pilot` for CPU qualification, `support` for the GPU experiment's CPU host, or `gpu` to add the spot GPU. Supply `run_id`, UTC `start` and `cutoff` ending in Z, `project_id`, `subnet_id`, `cluster_id`, `state_lineage`, `state_bucket`, `other_cost_usd` and the SHA-256 of the exact current ledger bytes in `ledger_sha256`. Supply `policy_id` for `support` and `gpu`. For the GPU stage record `support_group_id`, `support_instance_id` and `support_boot_disk_id` from the Ready CPU node, its provider inventory and its attached boot disk. Each host allows these three additional IDs during the GPU stage. Compare any approved retained CSI disks with the dated inventory before signing its digest. |
| Dated ledger | Supply `revision`, `run_id`, UTC `observed_at` (at most 15 minutes old) and `gpu_intervals` covering all allocations in this experiment. Each entry has `vm`, UTC `start`, and `end` (null while in flight). Overlaps count once per VM. Include posted and accrued-but-unposted spend through observation in `accrued_gpu_usd` and `accrued_non_gpu_usd`. |
| Remaining costs | Supply `cpu_and_boot_hourly_usd`, `gpu_boot_hourly_usd`, `tax_remaining_usd`, `other_remaining_usd` (retention, network, storage and other costs), `cleanup_other_usd` (additional shutdown costs) and `drain_hours` (before cutoff). The guard prices the support and disks through the extra release hour and separately reserves one GPU-hour at the $1.50 cap. Use `post_release_usd` for work priced after GPU drain. Count each expense once. |
| CPU pilot | Keep the window within eight hours. Set `remaining_gpu_hours` to the allowance reserved for later fits. The pilot prices CPU and boot SSD for the entire window plus one cleanup hour. Include accrued spend, tax, retention and cleanup. Its reservation records zero GPU-hours. The spot policy may be blocked during the pilot. |
| Fit measurements | Set `seed` to 27 or 28, `measurement_sha256` to the verified benchmark receipt digest and the observed `checkpoint_every` in updates. Record the trainer’s configured `save_interval_seconds` (default 900). The projection bounds additional timed saves by the available window. A zero interval prices a save at every update, including replay. Under `samples`, supply arrays of observed seconds for `update_seconds`, `fit_sample_pass_seconds`, `validation_pass_seconds`, `save_seconds`, `startup_restore_seconds` and `export_seconds`. Each evaluation pass covers its complete 6,664-window cohort. |
| Work after training | Under `post_work`, give `confirmation_5163`, `calibration_scoring`, `language` and `clean_reload` a measured `seconds_each` and `when` set to `allocated` or `after-release`. The latter also has `hourly_usd`. The projection prices four checkpoints for scoring, calibration and language tests plus one clean restore. |
| Retries | The operator can record verified progress under ledger `fit_progress["27" or "28"]["control" or "candidate"]`. Record `step`, `evaluated_through` (last fully evaluated scheduled step or -1), `checkpoint_sha256` and boolean `terminal`. A terminal arm needs `export_sha256`. With no record the checker prices the arm from zero. |

The fit receipt includes all three bounds, the ceiling counts, replay and component timings. Exit status 2 with `budget-stop` records that the measured work cannot fit. Before a real window the IAM and FinOps owners must seed the current operator-approved ledger at `prod/mk8s/release-control/ledger.json` in the versioned state bucket. Grant the provisioning identity conditional write to that key. Restrict each host’s timer prefix to its own cleanup identity for writes. The initial ledger contains `attempt_ids: []` and `active_reservation: null`. Scope also contains a unique `attempt_id`, the approved `baseline_sha256` and distinct Nebius account IDs in `cleanup_accounts` keyed by `bm` and `fw13`. Verify worker images, support preflights and MLflow readback at their designated release gates.

In the funded ledger, record the IAM owner’s effective-grant evidence under `cleanup_access.bm` and `cleanup_access.fw13`. Each entry has `account_id`, the registered `public_key_id`, `project_id`, `cluster_id`, the approved `state_bucket` and `state_key`, UTC `checked_at` and `valid_through`, the SHA-256 of the archived IAM review in `evidence_sha256`, and verified booleans `node_group_delete`, `state_write` and `lock_write_delete`. The end time must cover the extra release hour. Archive the actual role and membership records with that digest. The locked rehearsal tests the live state lock. Provider deletion and Terraform state persistence are exercised during the authorized worker release.

Install the checked release files from this commit at `/opt/ml-infra/bootstrap/nebius` on bm, including `worker_cleanup.py`, `expire-workers`, `with-credentials`, `main.tf`, `versions.tf`, the lockfile and `management/publish-timer-attestation`. Install the expiry service and timer templates from the same commit and run `systemctl daemon-reload` after unit changes.

Install [the publisher](publish-timer-attestation) and [its oneshot unit](worker-attest@.service) beside the cleanup units. The publisher needs system Python with boto3 and StateDirectory access. Supply the approved JSON as `/etc/nebius-cleanup/RUN_ID-scope.json`, readable by the cleanup account. With both schedulers armed, run `systemctl start worker-attest@RUN_ID.service` on bm just before apply. The fw13 job publishes from a controller-scheduled tick. The bm publisher checks that Terraform and the authenticated Nebius CLI select the same enrolled account, key ID and private key file. It compares the complete cloud inventory to the approved IDs, verifies the effective systemd service and timer, and runs the same locked, read-only targeted destroy plan used at cutoff. The reservation checks the shared release source and scope digests, the bm unit files and the pinned fw13 CronJob identity, spec and image. A pilot rehearsal expects zero run-owned groups. A GPU-stage rehearsal must return the support group’s ID. At cutoff, Terraform targets the two worker addresses and the guard requires every proposed deletion to match an independently observed live group ID and ownership labels. If a partial creation left a run-owned group outside state, cleanup releases the verified managed groups, reports the orphan and exits unsuccessfully so both timers stay armed. The incident owner reconciles the orphan with the provider. Unrelated foundation changes stay outside the release plan. It writes a versioned proof under `prod/mk8s/release-control/timers/RUN_ID/STAGE/HOST.json`.

After human release checks, run `./with-credentials ./check-gpu-plan apply "$plan" --scope "$scope" --ledger "$ledger" --receipt "$prepare_receipt"` from the trusted provisioning host. This is the mutating command. It checks the private copy of the plan, verifies the exact live ledger bytes and both current host proofs, then uses S3 `If-Match` to reserve the attempt before executing the same copy. Collect the reservation version and provider IDs. An active hold, duplicate attempt, missing/stale evidence or concurrent ledger update blocks the apply. On a failed or ambiguous apply, retain the hold and inspect provider operations and inventory. FinOps closes it with a conditional update after reconciling actual charges and outstanding commitments, retaining the attempt ID history. Issue a new ledger revision and approval for the next stage. The fit projector supplies decision receipts. After reconciling the allocation hold, use the attempt reservation below for each production paired fit.

### Fund a paired training attempt

Use the management runner and its scoped training submitter. The episode launcher renders a CPU preflight and a GPU workload. On Nebius, both the native Job and SkyPilot paths require a versioned ledger reservation between successful preflight and submission of the exact GPU render. Provide boto3 and the funding writer's S3 access on the management runner. Keep these credentials out of the SkyPilot backend and the rendered workload.

1. Render `episode-profile.py preflight` and `episode-profile.py job` or `sky` with identical profile arguments, including `--target prod-nebius`, immutable trainer/preflight images, the paired cohorts, cached weights and `--graceful-stop`. Save the exact outputs. Run `python3 bootstrap/nebius/fit_attempt.py describe PRE.json TASK.json` offline. The JSON identifies the run, seed, objective, inputs, checkpoint and hashes of both renders. A change of backend or deadline needs a fresh description.
2. FinOps verifies the actual single running GPU VM ID and reconciles its allocation into `gpu_intervals`. Retain the cumulative spend, attempt history and closed intervals. Write a new ledger revision with `fit_approval` containing the full `request` from step 1, the `vm` ID, the window's `cluster_id`, `project_id`, `start`, `cutoff`, `other_cost_usd`, the SHA-256 of the exact measurements file in `measurements_sha256`, and `max_attempts` including the first attempt. The `purpose` is `selected` for a 14,084-update ceiling. Use `qualification` for a shorter synthetic seed-27 fixture. Qualification reserves its full hard deadline and grace, needs a separate logical run, and earns zero selected-fit progress.
3. Set `NEBIUS_FIT_SCOPE` to the GPU scope file with the new exact ledger SHA-256 and `NEBIUS_FIT_MEASUREMENTS` to the measured timings file. Invoke `workloads/sky/launch-episode.sh --nebius job` or `sky` with the reviewed arguments. Capture the printed attempt ID, revision, S3 version and task hash. Each attempt recomputes the ceiling-based pair projection and checks the workload deadline against the hour, dollar and UTC limits. Keep the independent cutoff schedulers armed while the VM exists.
4. On success, interruption or ambiguous submission, leave the hold in place until the operator verifies the actual Job or SkyPilot cluster status, drains any remaining writer, and records the last recoverable generation or absence of one. An ambiguous conditional write also requires inspecting the authoritative ledger version before retry. Reconcile the attempt's cost, set `writer_fenced: true` and `closeout_sha256` (the verification evidence) in its `fit_attempts` entry, then clear `active_reservation` with a conditional ledger update. Preserve every `attempt_id`. If funding was denied after a successful CPU preflight, collect its receipt and remove the completed CPU Job before resubmitting the same ordinal.
5. For a selected retry, link the verified arm record in `fit_progress` to the originating `attempt_id`. Supply `--resume --resume-checkpoint STEP:SHA256` matching that record. The worker rechecks the manifest, run identity and the trainer's recoverable generation under its writer lock. If recovery fell back, reconcile the older valid generation and reprice before retry. If no generation survived, first fence the old writer. On the retained claim, preserve the entire old run-state directory under a quarantine name and prepare an empty directory with the original owner, mode and mount path. From the management checkout with that claim mounted, save the output of `python3 bootstrap/nebius/fit_attempt.py check-fresh PATH --after-attempt CONSUMED_ID` and add its SHA-256 to the closed attempt as `fresh_state_sha256`. Verify the mounted path belongs to that run. The probe rejects leftover generations, hidden receipts and partial files. Omit the arm progress record and both resume flags to charge a fresh replay. Keep the quarantined state through closeout. A qualification retry can instead record `qualification_checkpoint` as `STEP:SHA256` in its last closed qualification attempt. Keep qualification retries on the same schedule. Keep each seed/arm's logical run ID and the pair's image and input pins stable, and increment `--attempt` exactly once per consumed reservation.
6. Before seed 28, record `seed_28_decision` with the seed-27 `exports` indexed by `control` and `candidate`, the decision's `evidence_sha256`, `high_target_blocks`, `control_high_mae`, `candidate_high_mae`, `control_global_mae` and `candidate_global_mae`. Both exports must be terminal. The runner checks strict high-hour improvement, non-worsening global MAE and at least four high-target blocks before pricing the new pair. A budget or replication refusal leaves the next attempt unconsumed.
