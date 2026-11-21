#!/usr/bin/env python3
"""Verify reader labels against the approved render and Nebius binding metadata."""

import argparse
import json
from pathlib import Path


REGION = {"topology.kubernetes.io/region": "eu-south1"}
PHASE = "airaware.training/phase"


def verify_labels(expected, observed):
    if (expected.get(PHASE) != "checkpoint-reader"
            or REGION.keys() & expected.keys()
            or observed not in (expected, expected | REGION)):
        raise ValueError("Reader labels differ from the approved reader and managed region")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("approved_pod", type=Path)
    parser.add_argument("observed_pod", type=Path)
    args = parser.parse_args()
    expected, observed = [json.loads(path.read_bytes()) for path in (args.approved_pod, args.observed_pod)]
    verify_labels(expected["metadata"]["labels"], observed["metadata"]["labels"])


if __name__ == "__main__":
    main()
