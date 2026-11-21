"""Check the OCI index, linux/amd64 image and attached BuildKit provenance."""

import hashlib
import json
import re


def verified_config(layout, receipt):
    def blob(descriptor):
        digest = descriptor["digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("Expected an OCI SHA-256 digest")
        path = layout / "blobs" / "sha256" / digest[7:]
        with path.open("rb") as data:
            if path.stat().st_size != descriptor["size"] or hashlib.file_digest(data, "sha256").hexdigest() != digest[7:]:
                raise ValueError(f"Size or digest mismatch for {digest}")
        return path

    root, = json.loads((layout / "index.json").read_bytes())["manifests"]
    if root["digest"] != receipt["index"]:
        raise ValueError("Pulled index differs from qualified index")
    manifests = json.loads(blob(root).read_bytes())["manifests"]
    platform, = (m for m in manifests if m.get("platform") == {"os": "linux", "architecture": "amd64"})
    proof, = (m for m in manifests if m.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest")
    if len(manifests) != 2 or platform["digest"] != receipt["linux_amd64"] or proof["annotations"]["vnd.docker.reference.digest"] != platform["digest"]:
        raise ValueError("Unexpected platform or provenance target")

    image = json.loads(blob(platform).read_bytes())
    for layer in image["layers"]:
        blob(layer)
    config = json.loads(blob(image["config"]).read_bytes())["config"]
    if image["config"]["digest"] != receipt["config"]:
        raise ValueError("Unexpected image configuration digest")

    attestation = json.loads(blob(proof).read_bytes())
    blob(attestation["config"])
    statement_layer, = attestation["layers"]
    statement = json.loads(blob(statement_layer).read_bytes())
    subject, = statement["subject"]
    if statement["predicateType"] != "https://slsa.dev/provenance/v1" or subject["digest"]["sha256"] != platform["digest"][7:]:
        raise ValueError("Missing BuildKit provenance for the qualified platform")
    return config, len(image["layers"])
