# Stage public Flamingo weights in a successor allocation

These commands use the [public staging procedure](public-flamingo-stage.md) with a new, independently approved allocation. The original procedure keeps its existing deployment, CPU worker, scope and cutoff when invoked without window arguments.

## Who supplies the window

The allocation owner supplies the private scope file and approves its raw SHA-256, allocation run ID, UTC start, finite UTC cutoff and funded CPU support instance. The storage owner confirms continued funding and exclusive custody of the retained disk. The managed-cluster observer reads the actual training namespace UID and CPU Node name, UID and provider ID, confirms Linux/amd64, Ready, and the training CPU pool. The release owner supplies the installed signed deployment and release, including the paired preflight index and its Linux/amd64 manifest. Keep that deployment selected through receipt acceptance and guard retirement.

The existing authorized release issuer signs the exact bytes of `window.json` using the management host's enrolled release-signing identity and supplies the adjacent `window.json.sig`. An independent approver supplies the lowercase SHA-256 of those same JSON bytes through the approval channel. The verifier uses the installed `/etc/ml-infra/nebius-release/signer.pub` for both the window and the active signed deployment. Each operational role also needs its own scoped approval.

| Signed fields | Exact input |
|---|---|
| `schema`, `allocation_run_id`, `scope_sha256`, `start`, `cutoff` | Schema `public-flamingo-stage-window-v1` and independently approved raw scope bytes. The scope must identify a support or GPU allocation with matching `run_id`, `start`, `cutoff`, `project_id`, `cluster_id` and `support_instance_id`. Use UTC timestamps ending in `Z`. |
| `cpu_node`, `cpu_provider_id`, `cpu_node_uid` | Managed support instance name from the scope, observed provider ID and actual Kubernetes Node UID. |
| `deployment_commit`, `deployment_sha256`, `preflight_image`, `preflight_platform_image` | Exact commit and raw SHA-256 of the installed signed deployment, full repository references for its paired preflight OCI index and Linux/amd64 manifest. |
| `project_id`, `cluster_id`, `ca_sha256`, `namespace`, `namespace_uid`, `binding_sha256` | Enrolled managed target and original all-bound receipt. That receipt identifies the exact training PVC UID, PV UID and provider disk. |
| `promotion_sha256`, `objects_sha256`, `weights_manifest_sha256` | Original promotion receipt, exact nine-version object list and content-addressed weights leaf. The immutable target and source values are listed in [the verifier](public_flamingo_window.py). |

## Render and hand off

On the management host, set `window_sha256` and `deployment_sha256` from independent approvals. Set `window`, `scope`, `promotion` and `binding` to their private files. The following Bash commands produce a private review packet and a closed-guard render.

```bash
set -euo pipefail
set -C
umask 077
sky=workloads/sky
run="fsw-${window_sha256:0:32}"
state="$HOME/.local/state/ml-infra"
review_dir=$(mktemp -d "$state/$run.XXXXXXXX")
win=(--window "$window" --window-sha256 "$window_sha256" --window-scope "$scope")
render=(--run "$run" --inventory-secret "stage-$run-inventory" --expected-deployment "$deployment_sha256"
        --promotion "$promotion" --binding "$binding" "${win[@]}")
python3 "$sky/render-public-flamingo-stage.py" "${render[@]}" > "$review_dir/stage-review.json"
python3 "$sky/public_flamingo_stage_guard.py" --run "$run" --phase closed "${win[@]}" > "$review_dir/closed-guard.json"
sha256sum "$review_dir/stage-review.json" "$review_dir/closed-guard.json"
```

Give each operator and each reconciliation attempt a fresh private directory. Share exact evidence paths and independently pinned hashes. Keep every prior or partial receipt. A new local directory is a place to record observations. Before retrying any creation, reconcile the existing resource name, UID and custody. The content-derived run selects one output leaf and inventory Secret for this window.

Pass `"${win[@]}"` to **each** original command, including owner render/enroll/activate, consumer check, inventory issue, stage enroll/activate, all three guard phases and capture/fence. Commands taking `--scope` also receive that same successor scope. Add `window_sha256` to each role approval, the version reader grant, and every guard pin record, including the nested records in activation and closure. The stage Job, program ConfigMap and inventory Secret carry the full window pin. The independently approved packet binds the exact worker bytes, immutable object list and both leaf mounts. Each identity verifying a live guard needs read access to the one named CPU Node and the training Namespace in addition to its existing scoped permissions.

The policy owner keeps the existing native hold in force and controls the single claim policy and binding across windows. Before changing a prior guard, accept its detached receipt, confirm the old inventory Secret and writers have retired, and transfer exclusive storage custody under the [guarded handoff procedure](public-flamingo-stage.md#retire-the-transaction-guard-for-the-baseline-handoff). If prior custody is unsettled, reconcile the prior writer before enrollment. Use fresh approvals, real Job/Secret UIDs and direct policy generations for the successor. The guard checks the API server's Pod creation timestamp against the signed start and the earlier of role expiry and allocation cutoff, reserving the Job deadline plus 120 seconds. Capture and fence while the window is valid. The next baseline owner must accept the detached receipt while the closed guard is effective.
