# Verify private forecast image candidates

The paired candidate record pins the trainer and CPU preflight from the same archive build. Use the existing private `ghcr.io/keneth-arias/ml-infra-qwen` package for both, with separate content digests and signature roles. The preflight contains the forecast input verifier. The trainer also contains the AirAware reader. The rootless CPU image has its own package and receipt.

Before publication, verify each saved OCI archive locally from the repository root. Set `role` to `trainer` or `preflight`, `archive` to its tested archive and `layout` to a fresh directory.

```sh
receipt=bootstrap/nebius/releases/paired-candidate.json
skopeo copy --all --preserve-digests "oci-archive:$archive" "oci:$layout:qualified"
python3 ci/verify-paired-image.py "$receipt" "$role" "$layout"
```

Publish the same bytes with the package publisher credential. Confirm that the destination package is private. Derive the candidate tag from its role and full index digest. If that tag exists, verify it resolves to the qualified digest before using it. Supply the existing candidate-signing key through the release operator's credential store.

```bash
set -euo pipefail
digest=$(jq -er --arg role "$role" '.images[$role].index' "$receipt")
repository=ghcr.io/keneth-arias/ml-infra-qwen
commit=$(jq -er .build_commit "$receipt")
tag="candidate-$role-${digest#sha256:}"
skopeo copy --all --preserve-digests "oci:$layout:qualified" "docker://$repository:$tag"
test "$(skopeo inspect --raw "docker://$repository@$digest" | sha256sum | cut -d' ' -f1)" = "${digest#sha256:}"
cosign sign --yes --tlog-upload=false --key "$release_key" \
  -a "role=$role" -a "build_commit=$commit" "$repository@$digest"
```

The **Verify private Nebius candidate images** workflow cold-pulls each role in a fresh runner with `packages: read`. The job checks the index, amd64 manifest, config, blobs and BuildKit statement. It verifies the trainer's three source revisions or the preflight's two source labels, then verifies the signature with its expected role and build revision. Grant the private infrastructure repository package Read access if the read-only job cannot resolve an image already verified by its publisher.

Record the three successful cold-pull jobs in the release record. Production admission still needs an independently trusted signer and approved role-to-digest mapping.
