#!/usr/bin/env python3
"""Verify a downloaded OCI image and its BuildKit provenance against the CPU pilot receipt."""

import argparse
import json
from pathlib import Path
from oci_candidate import verified_config


def verify(layout, receipt):
    if receipt["repository"] != "ghcr.io/keneth-arias/ml-infra-sky-cpu" or receipt["role"] != "rootless-cpu":
        raise ValueError("Expected the rootless CPU worker repository and role")

    config, layers = verified_config(layout, receipt)
    labels = config["Labels"]
    if config["User"] != "1000:1000":
        raise ValueError("Worker identity or configuration digest differs")
    if labels["org.opencontainers.image.revision"] != receipt["build_commit"] or labels["org.opencontainers.image.source"] != "https://github.com/keneth-arias/ml-infra":
        raise ValueError("Worker source labels differ")

    print(f"Verified {layers} layers, CPU worker identity and provenance for {receipt['index']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("layout", type=Path)
    args = parser.parse_args()
    verify(args.layout, json.loads(args.receipt.read_bytes()))
