# Rootless CPU worker for SkyPilot 0.14.3

This image and server adapter support one CPU head on linux/amd64 under Kubernetes Restricted Pod Security. The worker runs as UID/GID 1000, starts an ephemeral SSH host key and uses the Ray and SkyPilot dependencies installed at build time. The pod needs a writable home and temp directory and the pod-local `net.ipv4.ip_unprivileged_port_start=22` sysctl. Keep the image’s /run/sshd directory available. Ray uses the runtime file-descriptor limit at the qualified one-CPU scale. The server uses Kubernetes exec and port-forward to reach the worker.

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

The [qualification task](qualification-task.yaml) contains the pilot's placement, resource limits, runtime checks and pod security settings. The pod has a 15-minute qualification deadline and a 30-second termination grace period. Its service account is precreated. The worker starts the SSH server in the foreground after Ray initialization. SSH errors reach the pod log and server exit ends the head. Operators reach port 22 through the Kubernetes API. SSH-level TCP and Unix-socket forwarding are disabled. The worker uses its writable container home and temporary storage. Keep high availability disabled for this single head (pod restart policy `Never`). Set the dedicated API server’s configuration as follows.

```yaml
allowed_clouds: [kubernetes]
kubernetes:
  allowed_contexts: [prod-nebius]
  namespace: sky-cpu
  remote_identity: NONE
  context_configs:
    prod-nebius:
      ports: podip
```

SkyPilot 0.14.3 reaches SSH through Kubernetes port-forward. The task declares no application ports. On the existing local worker use its isolated test namespace, selector and imported image. On the managed cluster the `sky-cpu-pull` Secret supplies pull-only registry credentials. Supply the qualified image by digest when rendering the task.

```bash
index=$(jq -er '."containerimage.digest"' "$output/build.json")
build_commit=$(jq -er '."image.name" | capture(":git-(?<sha>[0-9a-f]{40})$").sha' "$output/build.json")
repository=ghcr.io/keneth-arias/ml-infra-sky-cpu
sed "s|REPLACE_WITH_QUALIFIED_DIGEST_REFERENCE|$repository@$index|" \
  bootstrap/nebius/management/rootless/qualification-task.yaml > "$output/task.yaml"
skopeo copy --all --preserve-digests --authfile "$private_authfile" \
  "oci-archive:$output/worker.tar" "docker://$repository:candidate-$build_commit"
skopeo inspect --authfile "$private_authfile" --raw "docker://$repository@$index" | sha256sum
```

Compare the registry index against the build receipt. Use separate pull-only credentials to fetch the index, platform manifest and blobs before pinning the approved digest in the production admission policy.

### Refresh the worker lock

Resolve on linux/amd64 with CPython 3.11 and the base image's pip. SkyPilot 0.14.3 requires Click below 8.2, which tightens the top-level cap. The qualified Pydantic 2.12.0 distribution requires exactly pydantic-core 2.41.1. Keep those versions together during updates. Downloading the committed hashes replays the qualified wheel set. For a dependency update, generate a fresh resolver report in a disposable environment and emit one wheel hash per resolved package. Preserve the separate build tools, review the diff, run the image's `pip check`, then repeat qualification.

```bash
dir=bootstrap/nebius/management/rootless
python3.11 -m pip install --dry-run --ignore-installed --only-binary=:all: \
  --report "$output/resolver.json" -r "$dir/requirements.in" setuptools wheel packaging
jq -r '.install[] | "\(.metadata.name | ascii_downcase | gsub("[-_.]+"; "-"))==\(.metadata.version) --hash=sha256:\(.download_info.archive_info.hashes.sha256)"' \
  "$output/resolver.json" | LC_ALL=C sort > "$output/requirements.proposed.txt"
```
