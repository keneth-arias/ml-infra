# HAMi on Nebius Managed Kubernetes

The production Flux controllers reconcile a Helm-rendered HAMi 2.11.0 deployment. The scheduler runs on the CPU pool. Its API admission webhook serves one namespace, `hami-workloads`. The `training` and `sky-cpu` namespaces keep their existing schedulers. Four logical GPU slots share one physical card under HAMi, with memory and core scaling both set to 1. Each workload requests its share explicitly. Run mutually trusted workloads on a shared card.

## Operator bootstrap

Use the production operator kubeconfig and pinned API environment on bm. Check the caller using `kubectl auth whoami` against the approved operator identity. From a reviewed and merged checkout, follow these steps. Inspect server-side dry runs for permission conflicts before applying the manifests.

1. Verify the dedicated HAMi node selector currently matches zero nodes. Record the identity of the original full-GPU pool, its static NVIDIA device plugin and its allocatable GPU count.
2. Apply `system-namespace.yaml` and `kubectl apply --server-side -k platform/hami/workloads` using the pinned production context. Inspect the workload admission policy's `status.typeChecking.expressionWarnings` and quota. The initial GPU quota is zero.
3. Run `platform/hami/webhook-tls prepare` and `platform/hami/webhook-tls install-secret`. These commands issue and verify a cluster-specific CA and a 180-day serving certificate for `hami-scheduler.hami-system.svc`. The CA private key stays under the operator's private state directory. The serving key is installed only in `hami-system/hami-scheduler-tls`. Include that state in protected operator backups.
4. Apply the exact chart runtime bindings from `system/runtime-access.yaml` as the bootstrap operator. Use the separate commit-pinned Flux source and named permissions to activate the core. [Flux handoff and rollback](../../bootstrap/nebius/management/hami/README.md)
5. Wait for the scheduler Deployment, its ready HTTPS endpoint and the matching Flux inventory. Run `platform/hami/webhook-tls install-webhook` and `platform/hami/webhook-tls verify`. The former inserts the public CA into the checked-in webhook template. Run `platform/hami/check-admission` to test TLS, mutation and managed API-server denial without a GPU allocation. Confirm a normal native training Pod keeps its original scheduler.

Run the certificate check before rotation. For routine renewal, sign a new serving certificate with the existing CA and the same SANs, install the new keypair in the Secret, roll the scheduler, and confirm the presented certificate and admission response before retiring the old keypair. For CA rotation, trust both public CAs before the serving-key rollout, then remove the old CA after verification. Keep the workload quota closed during a rotation without a healthy webhook replica.

## Enabling real shared GPUs

The current Nebius GPU image runs its NVIDIA device plugin as a file-backed static Pod. Removing its API mirror leaves the kubelet authoritative. Registering two pools of capacity for the same physical GPU would let a whole-GPU job and fractional jobs each claim the same memory. The standby DaemonSet matches only nodes carrying both `gpu=on` and `airaware.training/gpu-mode=hami`. Keep these labels off the existing funded worker.

Use a separately funded exclusive GPU pool or a scheduled replacement after its existing work is complete. Nebius documents creating a custom-plugin pool with managed GPU settings omitted. On that pool qualify a supported NVIDIA driver and container toolkit, install the `nvidia` runtime handler and RuntimeClass used by the workload manifests, and ensure HAMi is the only device allocator for its cards. Inventory the actual node, runtime class, driver, and device-plugin Pods after replacement. The named node selector, taints and a measured full GPU inventory are required before increasing the shared quota. [Nebius GPU setup](https://docs.nebius.com/kubernetes/gpu/set-up) · [HAMi prerequisites](https://project-hami.io/docs/installation/prerequisites)

The device plugin needs host mounts, host PID and elevated capabilities. The standby system namespace enforces Baseline. Enable the plugin on qualified nodes through a separately reviewed, operator-only host-level deployment boundary. Scope its credentials and privileged workload admission to the exact plugin on the dedicated pool. Keep user workloads under Restricted Pod Security.

Set quota from measured device capacity. Release one canary allocation, then verify two CUDA processes overlap on the same physical device within their respective core and memory requests. Verify excess allocations stay pending, clean up the test Pods, and confirm ordinary whole-GPU training still sees its expected capacity. The number of advertised slots alone does not prove parallel execution or enforcement. [HAMi Helm installation](https://project-hami.io/docs/get-started/deploy-with-helm)
