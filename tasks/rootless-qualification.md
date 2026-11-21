# Restricted CPU worker qualification on bm

On November 21, 2026 the worker built from the [pinned source revision](https://github.com/keneth-arias/ml-infra/commit/a56cf74b41bccb94c21df282ad959edf2ebc2736) ran on the existing local MicroK8s worker. The [candidate record](../bootstrap/nebius/releases/rootless-candidate.json) identifies the OCI index, linux/amd64 manifest, configuration and release role.

| Check | Observed result |
| --- | --- |
| Local SkyPilot 0.14.3 | The one-node Restricted task succeeded and emitted its log marker. SSH over Kubernetes port-forward authenticated as UID 1000. The second job entered RUNNING and then CANCELLED. |
| Teardown | After `sky down`, the namespace contained zero pods, Services and PVCs. |
| SSH supervision | A separate run of the same worker command logged the listening socket on stderr. TCP and Unix-socket forwarding were disabled. Terminating the SSH server ended the container. |
| Registry | The release workflow cold-pulls the private image by digest with a read-only job token and verifies its layers, BuildKit provenance and signed CPU-role claim. Use the check for the exact PR head as the registry receipt. |

Production admission still requires the approved signing identity and the complete release manifest. The managed worker has its own cold-pull, placement and shutdown checks.
