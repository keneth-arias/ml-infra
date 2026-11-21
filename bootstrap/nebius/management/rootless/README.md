# Rootless CPU worker for SkyPilot 0.14.3

This image and server adapter support one CPU head on linux/amd64 under Kubernetes Restricted Pod Security. The worker runs as UID/GID 1000, starts an ephemeral SSH host key and uses the Ray and SkyPilot dependencies installed at build time. The pod needs a writable home and temp directory and the pod-local `net.ipv4.ip_unprivileged_port_start=22` sysctl. The server uses Kubernetes exec and port-forward to reach the worker.

Build from a reviewed commit and keep the OCI archive through qualification and registry promotion. Supply a new output directory on a filesystem with space for the image. The build records the Git commit and repository in the image configuration. Its metadata file contains the immutable index digest. Requalify the emitted digest after a rebuild, including when the Debian package repositories have changed.

```bash
bootstrap/nebius/management/rootless/build-worker HEAD "$output"
docker load -i "$output/worker.tar"
jq -r '."containerimage.digest"' "$output/build.json"
```

Install the server dependencies into a dedicated CPython 3.11 virtualenv using the hashed linux/amd64 wheels in `requirements.txt`. Supply the separate build wheels in `build-wheels.txt` to the server’s offline pip during startup. The adapter verifies the installed upstream template and command-runner hashes, backs up the stock files and installs the rootless bootstrap. Run its check on each server start. Recreate the dedicated virtualenv from the lock to restore the upstream components.

```bash
python3 bootstrap/nebius/management/rootless/install-template.py "$server_venv"
python3 bootstrap/nebius/management/rootless/install-template.py --check "$server_venv"
```

Qualify the image on the local Kubernetes worker with one token-free pod, UID 1000, `allowPrivilegeEscalation: false`, all capabilities dropped and `RuntimeDefault` seccomp. Record the resolved index, platform manifest and config digest. Exercise job completion, logs, authenticated SSH over port-forward, job cancellation and teardown before promoting the identical OCI bytes to the private registry.
