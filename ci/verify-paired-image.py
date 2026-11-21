#!/usr/bin/env python3
"""Verify one downloaded forecast image against the qualified pair of images."""

import argparse
import json
from pathlib import Path
import re

from oci_candidate import verified_config


def verify(layout, receipt, role):
    if receipt["schema"] != "paired-forecast-candidate-v1" or set(receipt["images"]) != {"trainer", "preflight"}:
        raise ValueError("Expected the paired forecast release")
    for name in ("build_commit", "forecast_commit", "reader_commit"):
        if not re.fullmatch("[0-9a-f]{40}", receipt[name]):
            raise ValueError(f"Invalid commit for {name}")
    image = receipt["images"][role]
    if image["repository"] != "ghcr.io/keneth-arias/ml-infra-qwen":
        raise ValueError("Unexpected candidate repository")
    if receipt["images"]["trainer"]["index"] == receipt["images"]["preflight"]["index"]:
        raise ValueError("Each role requires a distinct image")
    config, layers = verified_config(layout, image)
    if role == "trainer":
        env = dict(field.split("=", 1) for field in config["Env"])
        actual = [env.get(key) for key in ("INFRA_SOURCE_REVISION", "FORECAST_SOURCE_REVISION", "AIRAWARE_SOURCE_REVISION")]
        expected = [receipt[key] for key in ("build_commit", "forecast_commit", "reader_commit")]
    else:
        labels = config["Labels"]
        actual = [labels.get(key) for key in ("org.opencontainers.image.revision", "airaware.forecast.revision")]
        expected = [receipt[key] for key in ("build_commit", "forecast_commit")]
    if actual != expected:
        raise ValueError(f"Unexpected build sources for {role}")
    print(f"Verified {role}, {layers} layers, build sources and provenance for {image['index']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("role", choices=["trainer", "preflight"])
    parser.add_argument("layout", type=Path)
    args = parser.parse_args()
    verify(args.layout, json.loads(args.receipt.read_bytes()), args.role)
