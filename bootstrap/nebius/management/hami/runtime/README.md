# Prepare the exclusive HAMi NVIDIA runtime

Use this optional runtime release during the next independently funded GPU window, after the native GPU worker has been released. The existing native worker plan selects a managed driver preset and installs Nebius's native GPU allocator. An exclusive HAMi window needs its own reviewed worker plan, funding and cleanup scope. Keep HAMi GPU admission closed and keep `gpu=on` and `airaware.training/gpu-mode=hami` **node labels** absent throughout runtime bootstrap. Keep the `airaware.training/gpu-mode=hami:NoSchedule` **taint** present from provisioning.

## Eligible node and version

Verify the managed cluster ID, endpoint, CA pin and operator identity on bm using the production [HAMi bootstrap](../README.md). Use a dedicated `gpu-rtx6000-a` group with `ubuntu24.04`, GPU settings omitted, and independently verified provider and Kubernetes node identities. Set `airaware.training/pool=training-hami` in the authorized worker plan. Add `airaware.training.node-restriction.kubernetes.io/gpu-mode=hami` through the operator after matching that identity. Confirm Node authorizer and NodeRestriction enforcement, both labels on exactly the approved exclusive node, and the dedicated taint. On any concurrently present native node, confirm the two labels are absent and `nvidia.com/gpu.deploy.operands=false` is already present. Abort on unapproved nodes, NFD or GPU Operator ownership conflicts, any earlier HAMi DaemonSet still matching this node, or a native NVIDIA allocator on the exclusive node, including static mirror Pods. [Node isolation and NodeRestriction](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#node-isolation-restriction)

Nebius's compatibility API returned `ubuntu24.04` with no driver preset for `gpu-rtx6000-a` on Kubernetes 1.36. It also returned the native managed presets `cuda13.0` and `nvidia-latest`. This release uses the first option. The managed cluster reported Kubernetes 1.36.3 and the nodes reported containerd 2.3.3. NVIDIA GPU Operator 26.3 supports Kubernetes 1.32–1.36 and containerd 1.7–2.3 on Ubuntu 24.04. The RTX PRO 6000 Blackwell Server Edition minimum driver is 575.57.08. Debian is outside the Nebius MK8s compatibility result and the NVIDIA Operator OS matrix for this path. The node kernel must have matching accessible headers for the selected non-precompiled R580 open kernel module. The previously observed Nebius kernel, `6.11.0-1016-nvidia`, needs an explicit header check on the new node. Confirm registry and kernel package egress. The runtime uses the public Data Center GPU Driver and HAMi CUDA sharing, with no NVIDIA vGPU guest driver or NVIDIA License System configuration. If Nebius classifies the new preset as licensed vGPU, stop and confirm the entitlement and driver path before use. [Nebius GPU setup and compatibility matrix](https://docs.nebius.com/kubernetes/gpu/set-up#gpu-drivers-and-other-components) · [NVIDIA 26.3 platform support](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.3/platform-support.html) · [NVIDIA vGPU for Compute licensing](https://docs.nvidia.com/ai-enterprise/release-7/latest/infra-software/vgpu/licensing.html)

## Review and render

The Nebius public OCI registry serves the GPU Operator `v26.3.3` release with OCI manifest digest `sha256:9e163929dcf046b4e3de0f470887b19c5abd1aa81fe77181fe3755452ec2373e`. The documentation's v25.10.0 example predates support for the observed Kubernetes and containerd versions. The renderer checks the actual archive SHA-256 before executing Helm, then renders for the supplied 1.36 patch version. Use the exact reviewed archive and values for deployment. [Nebius custom GPU Operator setup](https://docs.nebius.com/kubernetes/gpu/set-up#how-to-install-the-drivers-and-components-on-existing-node-groups-cli) · [HAMi prerequisites](https://project-hami.io/docs/installation/prerequisites)

From the signed `ml-infra` deployment checkout, with the managed kubectl command in array `k` and a private review directory in `review`:

```bash
runtime=bootstrap/nebius/management/hami/runtime
chart_repo=oci://cr.eu-north1.nebius.cloud/marketplace/nebius/nvidia-gpu-operator/chart/gpu-operator
helm pull "$chart_repo" --version v26.3.3 --destination "$review"
chart="$review/gpu-operator-v26.3.3.tgz"
version=$("${k[@]}" version -o json | jq -er '.serverVersion.gitVersion')
"$runtime/render" "$chart" "$version" >"$review/nvidia-gpu-operator.yaml"
HAMI_RUNTIME_CHART="$chart" python3 -m unittest discover -s ci -p test_hami_runtime.py
"${k[@]}" apply --dry-run=server -f "$runtime/daemonset-fence.yaml"
```

Expected Nebius registry manifests observed for the archive's required images:

| Image and tag | Manifest SHA-256 |
| --- | --- |
| `driver:580.173.02-ubuntu24.04` | `75174d64e91534636d2f5546426537178448bec1e65d027c14e7f8b878c0525c` |
| `container-toolkit:v1.19.1` | `c927adbc9b7755c5cb90022fdcc5c1295f5fe5fe1f38200a2dc65e85632b029c` |
| `gpu-operator:v26.3.3` | `ba53a12f076411f3922a9f075df4dfa48b43e2d1860b2929150fe9b2cc75602d` |
| `node-feature-discovery:v0.18.3` | `f9ef2ebee55141a1758d3c0a87bb701f5db2adf6856f7218b11bc2bac7b63862` |
| `k8s-driver-manager:v0.11.0` | `9e8c0669fdabc5fec67638ffef8bdcce2d3cb38913f51eb7145b7d01671ab3f6` |

Compare the exact image manifests before deployment. The chart references tagged images and the archive check authenticates the reviewed chart bytes, including its bundled NFD chart. Record the live image IDs so the approval can attest the deployed images. Fetched image manifests establish availability from bm. The exclusive worker still needs working registry access.

## Stage during the funded window

Review the fence and operator in the signed release with the production GitOps operator. Stage the namespace and admission objects as the platform operator's separate bootstrap. The current named-object HAMi Flux grant covers the existing HAMi inventory. A separately approved GitOps delivery of the GPU Operator needs its own inventory, owner and privileges. The approved operator may manage the pinned chart as a separate Helm release once the fence is verified. Establish exactly one owner for the release and its cluster-scoped resources.

The reviewed installer requires explicit pins for the Kubernetes client and freshly enrolled identities. Set `OPERATOR_CONFIG`, `OPERATOR_CONTEXT`, `NEBIUS_OPERATOR_IDENTITY`, `NEBIUS_TRAINING_CLUSTER_ID`, `NEBIUS_CA_BUNDLE_SHA256`, and `NEBIUS_API_ENDPOINT` from the approved cluster and operator receipt. Set `HAMI_INSTANCE_ID` to the approved Compute VM ID independently matched to the enrolled node and funded worker scope. After approval, invoke the installer from the signed deployment checkout with those variables exported.

```bash
"$runtime/manage" install "$chart"
```

The installer requires native release, a Ready schedulable CPU worker, and the exclusive idle GPU node. It checks existing NFD and GPU Operator workloads and API discovery before creating anything. It copies and validates the pinned chart in a private staging directory, applies the fence, and requires a managed server-side dry run to add both selectors and preserve an existing OS selector. Helm then installs the local archive as the sole owner of the release. Use Helm's first-install CRD and hook handling. Applying the rendered file directly would also submit its chart hook objects. Review hook and CRD ownership separately on recovery or removal. The installer stops for existing GPU Operator/NFD APIs, including CRDs that may persist after an earlier Helm uninstall.

The stock Nebius chart favors a control-plane node through a soft preference and has no GPU requirement for the controller. This release explicitly selects the existing `training-cpu` pool for the GPU Operator controller, while NFD worker and all GPU operands use the fenced pool. NFD master and garbage collector are ordinary CPU Deployments. No existing NFD/GPU Operator DaemonSet, Deployment or matching APIs were visible on the managed cluster during the read-only discovery, and the installer repeats that check for the actual window.

Inspect the rendered cluster policy, dedicated driver selector, RBAC and DaemonSet in the approved review. Keep the standard NVIDIA device plugin, GFD and MIG manager disabled. The Operator's NFD worker publishes the PCI discovery label on the exclusive node. It can then reconcile the driver, toolkit and `nvidia` RuntimeClass. The NVIDIA toolkit sets the actual containerd handler, with `runc` remaining the default. The Operator's validator is configured without a CUDA workload. [Kubernetes MutatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/mutating-admission-policy/) · [GPU Operator installation](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/26.3/getting-started.html)

## Verify and hand off

Require the sole approved node to have the NVIDIA PCI feature label, Ready driver and toolkit DaemonSets, both placement selectors on all operator DaemonSet templates, and all of their Pods placed only on that exact node. Require the ClusterPolicy status `ready`, absence of the native NVIDIA registrar and its `nvidia.com/gpu` capacity on the exclusive node, and the cluster RuntimeClass `nvidia` with `handler: nvidia`. Use approved node readback to verify that containerd's effective configuration contains the `nvidia` handler and that the binary/correct driver root exist. A RuntimeClass object alone is insufficient. Collect the runtime verification receipt while the legacy activation labels are still absent.

```bash
"$runtime/manage" verify >"$review/hami-runtime-receipt.json"
```

The verifier checks identity, node/Pod placement, operator and operand readiness, absence of a native or HAMi GPU registrar, the actual RuntimeClass, and the presence of `/run/nvidia/validations/toolkit-ready` inside the Ready validator on the approved node. It checks that the validator uses RuntimeClass `nvidia` and the marker is read through its actual host mount. Its only remote exec runs the read-only file test in that validator. The receipt includes node identity, containerd version and image IDs. Kubernetes admission and Helm render tests cannot attest successful kernel driver installation or GPU access.

Only after runtime readback and after verifying the old device plugin remains at zero or has been retired, have the operator add the legacy activation labels according to the approved device-plane procedure. Use only the funded managed Nebius pool for follow-up GPU probes. Keep workload admission closed until the device-plane checks pass. If bootstrap fails, preserve the fence, keep both activation labels absent and close out the approved exclusive worker. Keep the fence in place until all GPU Operator operands and DaemonSets are gone. Follow the independently armed cutoff cleanup. Leave deployment pending if the new scope, headers, runtime path or exclusive capacity cannot be verified.
