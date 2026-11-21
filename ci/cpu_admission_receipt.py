"""Resolve the rendered CPU policy to its immutable qualification receipt."""

import json
from pathlib import Path
import re
import subprocess
import sys

import yaml


def check_image(policy, receipt):
    repository = "ghcr.io/keneth-arias/ml-infra-sky-cpu"
    if (
        receipt["repository"] != repository
        or receipt["role"] != "rootless-cpu"
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", receipt["index"])
    ):
        raise ValueError("Expected the qualified rootless CPU role, repository and index")
    expressions = [v["expression"] for v in policy["spec"]["variables"] if v["name"] == "skyImage"]
    if expressions != [repr(f"{repository}@{receipt['index']}")]:
        raise ValueError("CPU admission image differs from its qualification receipt")


def resolve(output):
    data = subprocess.check_output(["kubectl", "kustomize", "platform/overlays/prod/sky-cpu"])
    policy, = (
        obj for obj in yaml.safe_load_all(data)
        if obj["kind"] == "ValidatingAdmissionPolicy" and obj["metadata"]["name"] == "sky-cpu-workload"
    )
    revision = policy["metadata"]["annotations"]["airaware.training/qualification-revision"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected an immutable qualification commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", revision, "HEAD"], check=True)
    raw = subprocess.check_output(["git", "show", revision + ":bootstrap/nebius/releases/rootless-candidate.json"])
    check_image(policy, json.loads(raw))
    output.write_bytes(raw)
    print(revision)


if __name__ == "__main__":
    resolve(Path(sys.argv[1]))
