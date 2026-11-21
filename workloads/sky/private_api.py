"""Verify the selected Nebius Kubernetes endpoint and CA before using a credential."""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys


def api_host(cluster_id, endpoint):
    if not re.fullmatch(r"mk8scluster-[a-z0-9]+", cluster_id):
        raise ValueError("Invalid Nebius cluster ID")
    if endpoint not in ("public", "private"):
        raise ValueError("Select the public or private Kubernetes endpoint")
    prefix = "pu" if endpoint == "public" else "pr"
    return f"{prefix}.{cluster_id}.mk8s.eu-south1.nebius.cloud"


def pinned_kubeconfig_ca(
    cmd, cluster_id, expected_pin, *, endpoint="private", direct=False
):
    # Limit output to public transport metadata, excluding the user credential.
    cluster = json.loads(
        subprocess.check_output(
            [
                *cmd,
                "config",
                "view",
                "--minify",
                "--raw",
                "--flatten",
                "-o",
                "jsonpath={.clusters[0].cluster}",
            ]
        )
    )
    host = api_host(cluster_id, endpoint)
    if (
        cluster["server"] != f"https://{host}:443"
        or cluster.get("insecure-skip-tls-verify")
        or cluster.get("tls-server-name", "") not in ("", host)
        or (direct and cluster.get("proxy-url"))
    ):
        raise ValueError("Unexpected Kubernetes target or TLS configuration")
    ca = base64.b64decode(cluster["certificate-authority-data"], validate=True)
    if not ca or hashlib.sha256(ca).hexdigest() != expected_pin:
        raise ValueError("Unexpected Kubernetes CA bundle")
    return ca


if __name__ == "__main__":
    # The production shell launcher supplies its explicit kubectl command and context.
    pinned_kubeconfig_ca(
        sys.argv[1:],
        os.environ["NEBIUS_TRAINING_CLUSTER_ID"],
        os.environ["NEBIUS_CA_BUNDLE_SHA256"],
        endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"),
        direct=True,
    )
