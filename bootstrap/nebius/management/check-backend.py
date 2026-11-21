"""Validate the dedicated SkyPilot service environment before opening its listener."""

import hashlib
import json
from importlib.metadata import version
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import yaml

MANAGEMENT = Path(__file__).resolve().parent
sys.path.insert(0, str(MANAGEMENT.parents[2] / "workloads/sky"))
from production_target import backend_target, kubectl_client


def check_config(env, contexts, installed_version):
    actual = yaml.safe_load(Path(env["SKYPILOT_CONFIG"]).read_bytes())
    qualified = yaml.safe_load((MANAGEMENT / "config.yaml").read_bytes())
    if (
        installed_version != "0.14.3"
        or "SKYPILOT_ALLOW_ALL_KUBERNETES_CONTEXTS" in env
        or "SKYPILOT_API_SERVER_ENDPOINT" in env
        or env["KUBECONFIG"] != env["NEBIUS_SUBMIT_KUBECONFIG"]
        or contexts != ["prod-nebius"]
        or actual != qualified
    ):
        raise ValueError(
            "Invalid production SkyPilot version, context or server settings"
        )


def check_wheels(env, lock):
    if env.get("PIP_NO_INDEX") != "1":
        raise ValueError("The pilot requires offline build wheels")
    expected = {}
    for line in lock.read_text().splitlines():
        pin = re.fullmatch(
            r"([a-z0-9_]+)==([0-9.]+) --hash=sha256:([a-f0-9]{64})", line
        )
        if not pin:
            raise ValueError("Invalid qualified build-wheel pin")
        name, release, digest = pin.groups()
        expected[f"{name}-{release}-py3-none-any.whl"] = digest
    wheels = Path(env["PIP_FIND_LINKS"])
    if not expected or set(expected) != {p.name for p in wheels.iterdir()}:
        raise ValueError("Wheelhouse files differ from the qualified build wheels")
    for name, digest in expected.items():
        path = wheels / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected a regular build wheel: {name}")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Build wheel digest differs: {name}")


def main():
    if any(
        shutil.which(tool) is None
        for tool in ("kubectl", "ssh", "rsync", "socat", "nc")
    ):
        raise ValueError("Install the pilot Kubernetes and SSH client tools")
    contexts = subprocess.check_output(
        ["kubectl", "config", "get-contexts", "-o", "name"], text=True, timeout=15
    ).splitlines()
    check_config(os.environ, contexts, version("skypilot"))
    check_wheels(os.environ, MANAGEMENT / "rootless/build-wheels.txt")
    kubectl_client("sky-cpu", "sky-backend", direct=True)
    # The authenticated proxy serves this nonsecret record while the unit is running.
    directory = Path(os.environ["RUNTIME_DIRECTORY"])
    target = directory / "target.json.new"
    target.write_text(json.dumps(backend_target("sky-cpu")))
    target.chmod(0o644)
    target.replace(directory / "target.json")


if __name__ == "__main__":
    main()
