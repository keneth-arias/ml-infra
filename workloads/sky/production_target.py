"""Verify production Kubernetes identity, API TLS pin and the scoped SkyPilot endpoint."""

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

from private_api import pinned_kubeconfig_ca


def kubectl_client(namespace, account, *, direct=False):
    return kubectl_client_for_identity(namespace, f"system:serviceaccount:{namespace}:{account}", direct=direct)


def kubectl_client_for_identity(namespace, expected_identity, *, direct=False):
    file = Path(os.environ["NEBIUS_SUBMIT_KUBECONFIG"])
    if file.stat().st_mode & 0o077:
        raise ValueError("Submit kubeconfig must be private")
    cmd = ["kubectl", "--kubeconfig", str(file), "--context", "prod-nebius"]
    pinned_kubeconfig_ca(
        cmd,
        os.environ["NEBIUS_TRAINING_CLUSTER_ID"],
        os.environ["NEBIUS_CA_BUNDLE_SHA256"],
        endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"),
        direct=direct,
    )
    configured = subprocess.check_output(
        [
            *cmd,
            "config",
            "view",
            "--minify",
            "-o",
            "jsonpath={.contexts[0].context.namespace}",
        ],
        text=True,
    )
    if configured != namespace:
        raise ValueError("Unexpected Kubernetes namespace")
    identity = subprocess.check_output(
        [
            *cmd,
            "--request-timeout=10s",
            "auth",
            "whoami",
            "-o",
            "jsonpath={.status.userInfo.username}",
        ],
        text=True,
    )
    if not expected_identity or identity != expected_identity:
        raise ValueError("Unexpected Kubernetes submitter")
    return [*cmd, "-n", namespace]


def require_sky(namespace):
    # Separate administrator-supplied approved endpoint for each server namespace.
    key = "NEBIUS_CPU_SKY_URL" if namespace == "sky-cpu" else "NEBIUS_TRAINING_SKY_URL"
    expected = os.environ[key]
    url = urlsplit(expected)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.hostname in ("localhost", "127.0.0.1", "::1")
        or url.username
        or url.password
        or url.query
        or url.fragment
        or os.environ.get("SKYPILOT_API_SERVER_ENDPOINT") != expected
    ):
        raise ValueError("Select the approved authenticated HTTPS endpoint")
    info = json.loads(
        subprocess.check_output(["sky", "api", "info", "-o", "json"], timeout=30)
    )
    if (
        info["server"]["url"].rstrip("/") != expected.rstrip("/")
        or info["server"]["status"].lower() != "healthy"
        or info["server"]["version"] != "0.14.3"
        or info["client"]["version"] != "0.14.3"
    ):
        raise ValueError("Unexpected SkyPilot endpoint, health or version")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-training-submit"]:
        kubectl_client("training", "episode-submit", direct=True)
    else:
        require_sky(sys.argv[1])
