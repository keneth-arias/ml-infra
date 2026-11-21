# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Select the two Flux 2.10.0 controller workloads for the private CPU pool."""
import hashlib
import json
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
MANIFEST = HERE.parents[3] / "clusters/dev-bm/flux-system/gotk-components.yaml"
IMAGES = {
    "source-controller": "611b6bca3eae95bcff471414d1fb14a4ab83d7f14e609cccef2bd389c121f37e",
    "kustomize-controller": "37db29b25dae35b881a5e064acedb0cc954442da453a39eaa0eab7a90dbecc4e",
}


def render():
    manifest = MANIFEST.read_bytes()
    if hashlib.sha256(manifest).hexdigest() != "8415fe224f30aac504709ca7b5d968e4e8715eed13df97e9ba7a5eee007a4952":
        raise ValueError("Review the changed upstream Flux manifest before using it in production")
    objects = []
    for obj in yaml.safe_load_all(manifest):
        if not obj:
            continue
        kind, name = obj["kind"], obj["metadata"]["name"]
        if kind == "NetworkPolicy" and name == "allow-egress":
            objects.append(obj)
        if name not in IMAGES or kind not in ("ServiceAccount", "Deployment", "Service"):
            continue
        if kind == "Deployment":
            pod = obj["spec"]["template"]["spec"]
            pod.pop("priorityClassName")
            pod.setdefault("nodeSelector", {})["airaware.training/pool"] = "training-cpu"
            container, = pod["containers"]
            container["image"] = f"ghcr.io/fluxcd/{name}@sha256:{IMAGES[name]}"
            args = container["args"]
            args[args.index("--watch-all-namespaces=true")] = "--watch-all-namespaces=false"
            if name == "kustomize-controller":
                args.extend(["--default-service-account=nebius-platform", "--no-cross-namespace-refs=true",
                             "--no-remote-bases=true", "--feature-gates=DisableConfigWatchers=true"])
        objects.append(obj)
    objects.extend(yaml.safe_load_all((HERE / "access.yaml").read_bytes()))
    return objects


if __name__ == "__main__":
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": render()}))
