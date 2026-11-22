# /// script
# requires-python = ">=3.11"
# dependencies = ["kubernetes-validate==1.36.0", "PyYAML==6.0.3", "jsonschema==4.26.0"]
# ///
"""Render each cluster and its Flux overlays, and validate their objects offline."""
from pathlib import Path
import re
import subprocess

import jsonschema
import kubernetes_validate
import yaml


def render(path):
    output = subprocess.check_output(["kubectl", "kustomize", str(path)], text=True)
    return [resource for resource in yaml.safe_load_all(output) if resource]


def validate_cluster(cluster, *, production):
    minor = "1.36" if production else "1.35"
    objects = render(cluster)
    paths = {
        obj["spec"]["path"] for obj in objects
        if obj["kind"] == "Kustomization" and obj["spec"]["sourceRef"]["kind"] == "GitRepository"
    }
    for path in sorted(paths):
        if Path(path) != cluster:
            objects.extend(render(path))

    schemas = {}
    for obj in objects:
        if obj["kind"] == "CustomResourceDefinition":
            spec = obj["spec"]
            for version in spec["versions"]:
                schemas[(f"{spec['group']}/{version['name']}", spec["names"]["kind"])] = version["schema"]["openAPIV3Schema"]

    for obj in objects:
        if obj["kind"] == "Secret":
            if "sops" not in obj or not all(
                isinstance(value, str) and value.startswith("ENC[")
                for field in ("data", "stringData") for value in obj.get(field, {}).values()
            ):
                raise ValueError(f"Expected SOPS-encrypted Secret: {obj['metadata']['name']}")
            obj.pop("sops")
        if production:
            for image in images(obj):
                if not re.fullmatch(r".+@sha256:[0-9a-f]{64}", image):
                    raise ValueError(f"Production image requires a digest: {image}")
        schema = schemas.get((obj["apiVersion"], obj["kind"]))
        if schema:
            jsonschema.validate(obj, schema)
        else:
            version = kubernetes_validate.validate(obj, minor, strict=True)
            if version != minor:
                raise ValueError(f"Expected Kubernetes {minor} schemas, selected {version}")
    print(f"{cluster}: validated {len(objects)} objects")


def images(node):
    if isinstance(node, list):
        for item in node:
            yield from images(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key == "image" and isinstance(value, str):
                yield value
            else:
                yield from images(value)


if __name__ == "__main__":
    for cluster in sorted(Path("clusters").glob("*/kustomization.yaml")):
        validate_cluster(cluster.parent, production=cluster.parent.name.startswith("prod-"))
    validate_cluster(Path("platform/overlays/prod"), production=True)
    validate_cluster(Path("platform/overlays/prod/sky-cpu"), production=True)
    validate_cluster(Path("platform/hami/system"), production=True)
    validate_cluster(Path("platform/hami/workloads"), production=True)
    for path in ("scheduler", "device", "device/operator"):
        validate_cluster(Path("platform/hami") / path, production=True)
