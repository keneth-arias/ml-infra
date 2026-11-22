# Approve the three production images

The management host verifies two detached SHA-256 signatures with its enrolled production public key. The release manifest contains the complete candidate records for the rootless CPU worker and the Qwen trainer and preflight. A v2 release also binds the independent Flamingo trainer and preflight. The deployment receipt selects that manifest for one managed cluster and one ml-infra deployment commit. Production clients read the active receipt from the management host before submitting work.

## Prepare the release

After qualification, copy the exact OCI bytes to their private repositories and cold-pull each immutable reference with a pull-only identity. Verify the OCI index, amd64 manifest, config, provenance, role signature and source revisions for each role. Keep those receipts with the operator's release evidence. Use the rootless receipt selected by the deployment's admission policy and the paired candidate receipt for the approved build. The manifest command can also prepare an unsigned candidate before registry publication.

```sh
python3 workloads/sky/production_release.py manifest "$rootless_candidate" "$qwen_candidate" \
  --flamingo "$flamingo_candidate" > release.json
```

Enroll the release signer's public PEM independently at `/etc/ml-infra/nebius-release/signer.pub`. Compare its DER SHA-256 with the operator's enrollment record. Keep the signing key with the release operator. Anyone with access to deploy workload code or replace this trust directory can affect the client-side check. Use an operator-controlled checkout, a root-owned trust directory and release folders that submitters cannot write.

After verifying the reviewed deployment commit binds the approved image policy, create the receipt outside Git. Use the exact merged deployment commit on the production host, including its clean workload, bootstrap and deployment-policy sources. Record the source and build commits separately for the rootless and paired images in the release manifest. See the [field and format reference](https://docs.openssl.org/3.0/man1/openssl-dgst/) for the detached signature operations.

```sh
set -eu
deployment_commit=$(git rev-parse --verify HEAD)
release_sha256=$(sha256sum release.json | cut -d' ' -f1)
jq -n --arg digest "$release_sha256" --arg revision "$deployment_commit" --arg cluster "$NEBIUS_TRAINING_CLUSTER_ID" \
  '{schema:"nebius-image-deployment-v1", release_sha256:$digest, deployment_commit:$revision, cluster_id:$cluster}' > deployment.json
openssl dgst -sha256 -sign "$release_key" -out release.json.sig release.json
openssl dgst -sha256 -sign "$release_key" -out deployment.json.sig deployment.json
```

Install the two JSON files and their binary signatures in a new root-owned directory under `/etc/ml-infra/nebius-release/`. Select that directory by atomically replacing the `current` symlink. The signer stays outside the selected release directory. Verify the active release through the same entry point used by submissions.

```sh
python3 workloads/sky/production_release.py reference rootless-cpu
python3 workloads/sky/production_release.py reference preflight
python3 workloads/sky/production_release.py reference trainer
python3 workloads/sky/production_release.py reference flamingo-preflight
python3 workloads/sky/production_release.py reference flamingo-trainer
```

The native and SkyPilot training launchers check both rendered image roles before preflight and again before budget reservation. The standalone support-worker preflight uses the same checks. A change of deployment during preflight requires a fresh launch. SkyPilot also reads the authenticated backend target record at `/.well-known/nebius-target`. The CPU service writes this record after verifying its Kubernetes identity, cluster and CA at startup. A training backend must serve the same verified record for the training namespace before using SkyPilot there. The CPU admission probe and the guarded staging submitter use the same signed release. For a rollback, select the previously approved checkout, cluster policy and signed deployment together, then repeat the probes. Keep the training admission hold until the approved role and entry-point policies are in place.
