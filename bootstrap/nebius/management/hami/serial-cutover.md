# Switch from standby to one exclusive HAMi worker

Use the next funded GPU window after the native training worker is released. The release owner first settles the previous attempt, records the end of its GPU interval, and verifies the previous group, VM and boot disk are gone in Nebius. Preserve approved retained disks. Use the cumulative funding ledger and fresh bm and fw13 cleanup proofs for each allocation. The two-workload test consumes one physical GPU. The Helm charts, Kubernetes admission checks and work on bm consume zero GPU-hours.

## Split the existing Flux installation

The Baseline scheduler remains in `hami-system`. The upstream device plugin needs host access, so only its reviewed Pod template runs in `hami-device`. Fractional workloads run in the Restricted `hami-workloads` namespace. The operator owns the privileged namespace, runtime admission, webhook and RBAC. Flux reconciles the five scheduler objects and the five device objects with separate named identities. Both roots reuse the same commit-specific GitRepository.

1. In the pinned managed operator context, record the current Flux revisions, scheduler endpoint, node identities and legacy DaemonSet UID. Verify that zero nodes have both legacy activation labels, and that all HAMi resource quotas are zero. Before the first CPU worker of a new window is Ready, stateful Flux and webhook health checks may still be pending.
2. From the merged revision, render `render-flux.py "$commit" --plane scheduler` and `--plane device` with the `core`, `stage` and `access` subcommands. Compare the scheduler root with the installed standby root. It keeps the scheduler SA, both scheduler ConfigMaps, Service and Deployment. The extender switches its device-plugin namespace to `hami-device`. The device root renders the device ConfigMaps, Service, SA and DaemonSet. Verify the two device ConfigMaps against the pinned upstream chart.
3. Bootstrap the operator-owned device policies and grants from `kubectl kustomize platform/hami/device/operator`. Apply the policies and bindings before granting Flux access. Verify the managed API reports each policy observed at the current generation with zero type warnings. Apply the updated workload policy with the quota held at zero. Perform the scheduler handoff using the existing [Flux stage/fetch/enable procedure](README.md). Wait for its five-object inventory, the scheduler rollout, and a passing `platform/hami/check-admission`. During the switch, `prune: false` preserves the old objects.
4. Retire the old plugin while it still matches zero nodes. Confirm `nebius-hami` is Ready at the new revision and its inventory excludes the four old objects. Confirm the old DaemonSet has zero desired/current/ready Pods and no matching Pods exist. Remove its old ClusterRoleBinding `hami-device-plugin` after checking its subject is exactly `hami-system/hami-device-plugin`. Delete the old DaemonSet, ServiceAccount, ConfigMap (all named `hami-device-plugin`) and Service `hami-device-plugin-monitor` only in `hami-system`. For each deletion use the observed UID and resourceVersion as Kubernetes DeleteOptions preconditions. Keep `hami-scheduler-device` in that namespace. Confirm the old DaemonSet and old binding are absent.
5. While there are still zero nodes matching all four device selectors, seed the five device objects with the operator; establish the device-plane named Flux grant and activate its commit in the same way as the scheduler. The `--plane device core` output is the seed. Its DaemonSet must remain at zero desired/current while activation labels are absent. Never restore the standby Flux root while any exclusive worker has the activation labels.

## Prepare and qualify the managed worker

Use the serial allocation guard for a new `gpu_mode=hami` support and spot GPU window. Both cleanup schedulers stay armed. Match the worker's Nebius group and instance to the allocation receipt, the Kubernetes providerID and node UID. The group must omit the Nebius managed GPU preset. Verify Ready state, the `training-hami` pool label, the dedicated NoSchedule taint, zero native GPU capacity and absence of a native NVIDIA device registrar, including mirror Pods. An unexpected static registrar stops the handoff.

The operator applies the protected bootstrap label on the verified node. Supply the provider instance and node UID from independent evidence and review the patch first. Re-read the node and reconstruct the patch after any resourceVersion conflict; a changed providerID or UID requires a new qualification. The protected label binds runtime components to this worker. At this point the two legacy activation labels must still be absent.

~~~bash
node_patch() {
  jq -e --arg uid "$approved_uid" --arg provider "nebius://$HAMI_INSTANCE_ID" --argjson additions "$1" '
    select(.metadata.uid == $uid and .spec.providerID == $provider and
      .metadata.labels["airaware.training/pool"] == "training-hami") |
    [{op:"test",path:"/metadata/uid",value:$uid},
     {op:"test",path:"/metadata/resourceVersion",value:.metadata.resourceVersion},
     {op:"test",path:"/spec/providerID",value:$provider}] + $additions'
}
"${k[@]}" get node "$node" -o json | node_patch '[
  {"op":"add","path":"/metadata/labels/airaware.training.node-restriction.kubernetes.io~1gpu-mode","value":"hami"}]' >"$review/bootstrap-label.json"
"${k[@]}" patch node "$node" --type=json --patch-file "$review/bootstrap-label.json"
~~~

Install the reviewed Nebius distribution of NVIDIA GPU Operator v26.3.3 using the pinned archive and the [driver and Container Toolkit procedure](runtime/README.md). Run its managed verification and retain the receipt. Check the driver and Toolkit DaemonSets on this node, the validated runtime handler, the toolkit-ready marker and the absence of a competing device plugin. A successful management-host render only establishes which manifests will be installed.

After verifying the old DaemonSet is absent and the zero workload quota is effective, add the two device activation labels with another guarded patch.

~~~bash
"${k[@]}" get node "$node" -o json | node_patch '[
  {"op":"test","path":"/metadata/labels/airaware.training.node-restriction.kubernetes.io~1gpu-mode","value":"hami"},
  {"op":"add","path":"/metadata/labels/airaware.training~1gpu-mode","value":"hami"},
  {"op":"add","path":"/metadata/labels/gpu","value":"on"}]' >"$review/device-labels.json"
"${k[@]}" patch node "$node" --type=json --patch-file "$review/device-labels.json"
"${k[@]}" -n hami-device rollout status ds/hami-device-plugin --timeout=3m
~~~

Require exactly one Ready device Pod on the approved node, four allocatable `hami.io/gpu` slots, zero native slots, and usable HAMi node registration. Apply the bounded two-slot quota and run the [two CUDA probes](../../../../platform/hami/workloads/README.md). Capture their GPU UUID, assigned memory/core shares, overlapping execution and cleanup receipts. Restore the zero quota and close the funded reservation and worker interval with the actual provider shutdown time. On a failed prerequisite, keep the quota closed, capture the error, and release within the funded cutoff. A rollback to native requires retiring the dedicated worker and its runtime before returning to the standby root.
