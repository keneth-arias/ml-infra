#!/usr/bin/env python3
"""Verify a downloaded OCI image and its BuildKit provenance against the CPU pilot receipt."""

import argparse
import hashlib
import json
from pathlib import Path
import re


def verify(layout, receipt):
    if receipt["repository"] != "ghcr.io/keneth-arias/ml-infra-sky-cpu" or receipt["role"] != "rootless-cpu":
        raise ValueError("Expected the rootless CPU worker repository and role")

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
    labels = config["Labels"]
    if image["config"]["digest"] != receipt["config"] or config["User"] != "1000:1000":
        raise ValueError("Worker identity or configuration digest differs")
    if labels["org.opencontainers.image.revision"] != receipt["build_commit"] or labels["org.opencontainers.image.source"] != "https://github.com/keneth-arias/ml-infra":
        raise ValueError("Worker source labels differ")

    attestation = json.loads(blob(proof).read_bytes())
    blob(attestation["config"])
    statement_layer, = attestation["layers"]
    statement = json.loads(blob(statement_layer).read_bytes())
    subject, = statement["subject"]
    if statement["predicateType"] != "https://slsa.dev/provenance/v1" or subject["digest"]["sha256"] != platform["digest"][7:]:
        raise ValueError("Missing BuildKit provenance for the qualified platform")
    print(f"Verified {len(image['layers'])} layers, source {receipt['build_commit']}, provenance and index {root['digest']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("layout", type=Path)
    args = parser.parse_args()
    verify(args.layout, json.loads(args.receipt.read_bytes()))
