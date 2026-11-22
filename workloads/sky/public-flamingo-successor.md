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

## Qualify the CPU worker

The successor uses the repository's existing [protected Node-label procedure](../../bootstrap/nebius/management/hami/serial-cutover.md) on one CPU worker. The policy owner first installs the transaction's closed guard, compares its live policy and binding with the signed-window render, and verifies the managed API's admission behavior before the custodian marks a Node. It protects the two staging inputs and restricts the transaction's eligibility marker to the signed Node incarnation. An ordinary replacement can register with the same name. It lacks the required marker and cannot satisfy either writer's node selector. The scheduler and kubelet both check the same required node selector on the owner and stage Pods. The staging and source roles only read the marker. [Kubernetes kubelet admission](https://github.com/kubernetes/kubernetes/blob/v1.36.4/pkg/kubelet/lifecycle/predicate.go#L434-L447) · [Required Node affinity check](https://github.com/kubernetes/kubernetes/blob/v1.36.4/pkg/scheduler/eventhandlers.go#L751-L755)

The node custodian must independently authorize one metadata patch on the named Node and confirm Node authorizer and NodeRestriction are enforced. Keep the marker out of managed-pool templates, bootstrap and automatic relabeling. Use the enrolled CA-pinned management context for the custodian. Independently verify the window and the CPU's live name, UID, provider ID, pool, architecture, OS and Ready state. Review this patch and its server-side dry-run on that managed target before applying it. Keep the code checkout and input files private during the handoff.

```bash
# node_kube is the approved node custodian's kubectl argv for this managed cluster.
# node, node_uid and provider_id are independently enrolled fields of the verified window.
marker="airaware.training.node-restriction.kubernetes.io/$run"
node_receipt="$review_dir/node-before.json"
"${node_kube[@]}" get node "$node" -o json > "$node_receipt"
jq -en --arg uid "$node_uid" --arg provider "$provider_id" --arg node "$node" \
  --arg key "$marker" --slurpfile n "$node_receipt" '
  $n[0] as $n | $n.metadata as $m |
  select($m.name == $node and $m.uid == $uid and $n.spec.providerID == $provider and
         ($m.labels | has($key) | not) and ($m.deletionTimestamp == null)) |
  ($key | gsub("~"; "~0") | gsub("/"; "~1")) as $escaped |
  [{op:"test",path:"/metadata/uid",value:$uid},
   {op:"test",path:"/metadata/resourceVersion",value:$m.resourceVersion},
   {op:"test",path:"/spec/providerID",value:$provider},
   {op:"add",path:("/metadata/labels/"+$escaped),value:$uid}]' > "$review_dir/enroll-label.json"
"${node_kube[@]}" patch node "$node" --type=json --patch-file "$review_dir/enroll-label.json" \
  --dry-run=server -o json > "$review_dir/node-dry-run.json"
```

After separate approval, the custodian applies those exact patch bytes with the same preconditions and retains the returned UID and resourceVersion. On conflict, use a new evidence directory and repeat the identity checks. Keep the marker through the final fence recheck, receipt acceptance and guarded retirement. Then remove only this label using tests for the observed UID, current resourceVersion and exact label value. If the original Node has disappeared, record that fact and verify the replacement lacks eligibility. The reviewer must see the live managed dry-run and custody evidence before execution. [Kubernetes Node isolation](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#node-isolation-restriction)

## Transfer custody

The policy owner keeps the existing native hold in force and controls the single claim policy and binding across windows. Before changing a prior guard, accept its detached receipt, confirm the old inventory Secret and writers have retired, and transfer exclusive storage custody under the [guarded handoff procedure](public-flamingo-stage.md#retire-the-transaction-guard-for-the-baseline-handoff). If prior custody is unsettled, reconcile the prior writer before enrollment. Use fresh approvals, real Job/Secret UIDs and direct policy generations for the successor. The guard checks the API server's Pod creation timestamp against the signed start and the earlier of role expiry and allocation cutoff, reserving the Job deadline plus 120 seconds. Capture and fence while the window is valid. The next baseline owner must accept the detached receipt while the closed guard is effective.
