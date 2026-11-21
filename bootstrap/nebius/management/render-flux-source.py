#!/usr/bin/env python3
"""Render the production Flux source and reconciliation at an approved commit."""

import argparse
import json
from pathlib import Path
import re
import subprocess

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("commit", help="Reviewed, published 40-hex infrastructure Git commit")
p.add_argument("--fetch", action="store_true", help="Fetch the commit while reconciliation stays suspended")
a = p.parse_args()
if not re.fullmatch("[0-9a-f]{40}", a.commit):
    p.error("Supply the full immutable infrastructure revision")
root = Path(__file__).resolve().parents[3]
path = "clusters/prod-nebius"
for ref, expected in ((a.commit, "commit"), (f"{a.commit}:{path}/kustomization.yaml", "blob")):
    result = subprocess.run(["git", "-C", str(root), "cat-file", "-t", ref], capture_output=True, text=True)
    if result.returncode or result.stdout.strip() != expected:
        p.error(f"The local checkout must contain the commit and {path}/kustomization.yaml")
meta = {"name": "nebius-platform", "namespace": "flux-system"}
source = {
    "apiVersion": "source.toolkit.fluxcd.io/v1",
    "kind": "GitRepository",
    "metadata": {**meta, "name": "platform-" + a.commit},
    "spec": {
        "url": "ssh://git@github.com/keneth-arias/ml-infra.git",
        "interval": "10m",
        "ref": {"commit": a.commit},
        "secretRef": {"name": "nebius-git-read"},
        "suspend": not a.fetch,
    },
}
reconcile = {
    "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
    "kind": "Kustomization",
    "metadata": meta,
    "spec": {
        "sourceRef": {"kind": "GitRepository", "name": source["metadata"]["name"]},
        "path": "./" + path,
        "interval": "5m",
        "timeout": "5m",
        "serviceAccountName": "nebius-platform",
        "prune": False,
        "deletionPolicy": "Orphan",
        "suspend": True,
        "wait": False,
        "decryption": {"provider": "sops", "secretRef": {"name": "nebius-sops-age"}},
    },
}
# Delayed-binding PVCs remain Pending until an authorized worker and consumer exist.
print(
    json.dumps(
        {"apiVersion": "v1", "kind": "List", "items": [source, reconcile]}, indent=2
    )
)
