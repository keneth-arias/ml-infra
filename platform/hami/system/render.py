# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Reproduce the stage bundle from the pinned upstream Helm archive."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
import urllib.request

import yaml

DIRECTORY = Path(__file__).resolve().parent
CHART_URL = "https://project-hami.github.io/HAMi/charts/hami-2.11.0.tgz"
CHART_SHA256 = "2530d25ba1a953a5ffa8070c308a373f756c6108c5bb39fce49943f614dd9315"
NAMESPACE = "hami-system"
RBAC = {"ClusterRole", "ClusterRoleBinding", "Role", "RoleBinding"}
CORE = {"ConfigMap", "DaemonSet", "Deployment", "Service", "ServiceAccount"}
NVIDIA_RESOURCES = {"hami.io/gpu", "hami.io/gpumem", "hami.io/gpucores", "hami.io/gpumem-percentage", "hami.io/priority"}
HEADER = "# Generated from upstream HAMi chart 2.11.0 by render.py.\n"


class ManifestDumper(yaml.SafeDumper):
    def represent_str(self, value):
        style = "|" if "\n" in value else None
        return self.represent_scalar("tag:yaml.org,2002:str", value, style=style)


ManifestDumper.add_representer(str, ManifestDumper.represent_str)


def trim_runtime_access(resource):
    name = resource["metadata"]["name"]
    if name == "hami-scheduler":
        rule, = (rule for rule in resource["rules"] if "configmaps" in rule["resources"])
        rule["resources"].remove("configmaps")
        resource["rules"].append({"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list", "watch"]})
    elif name == "hami-device-plugin-monitor":
        # This deployment disables direct reporting of GPU memory/cores to Node status.
        rule, = (rule for rule in resource["rules"] if "nodes/status" in rule["resources"])
        rule["resources"].remove("nodes/status")


def render(archive):
    if hashlib.sha256(archive.read_bytes()).hexdigest() != CHART_SHA256:
        raise ValueError("HAMi chart archive SHA-256 differs from the pin")
    result = subprocess.check_output(
        ["helm", "template", "hami", str(archive), "--namespace", NAMESPACE,
         "--kube-version", "1.36.3", "-f", str(DIRECTORY / "values.yaml")],
        text=True,
    )
    core, access, webhook = [], [], []
    for entry in yaml.safe_load_all(result):
        kind = entry["kind"]
        name = entry["metadata"]["name"]
        if kind in CORE and entry["metadata"]["namespace"] == NAMESPACE:
            if kind in {"Deployment", "DaemonSet"}:
                spec = entry["spec"]["template"]["spec"]
                # Use normal priority so the add-on cannot preempt workloads.
                del spec["priorityClassName"]
                if kind == "Deployment":
                    spec["securityContext"] = {"seccompProfile": {"type": "RuntimeDefault"}}
                    for container in spec["containers"]:
                        container["securityContext"] = {
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        }
            if kind == "ConfigMap" and name == "hami-scheduler":
                config = yaml.safe_load(entry["data"]["config.yaml"])
                extender, = config["extenders"]
                extender["managedResources"] = [
                    resource for resource in extender["managedResources"] if resource["name"] in NVIDIA_RESOURCES
                ]
                if {resource["name"] for resource in extender["managedResources"]} != NVIDIA_RESOURCES:
                    raise ValueError("Missing reviewed NVIDIA scheduler resources")
                entry["data"]["config.yaml"] = yaml.safe_dump(config, sort_keys=False)
            if kind == "Service" and name == "hami-scheduler":
                # The extender's metrics listener is loopback only.
                entry["spec"]["ports"] = [port for port in entry["spec"]["ports"] if port["name"] == "http"]
            core.append(entry)
        elif kind in RBAC:
            if kind == "ClusterRole":
                trim_runtime_access(entry)
            access.append(entry)
        elif kind == "MutatingWebhookConfiguration" and name == "hami-webhook":
            admission, = entry["webhooks"]
            # Scope to the exact namespace and remove upstream label-based opt-outs.
            del admission["namespaceSelector"]["matchExpressions"]
            del admission["objectSelector"]
            webhook.append(entry)
        else:
            raise ValueError(f"Unexpected chart resource {kind}/{name}")
    if (len(core), len(access), len(webhook)) != (9, 8, 1):
        raise ValueError("The chart resource inventory differs from the reviewed bundle")
    return {"core.yaml": core, "runtime-access.yaml": access, "operator/webhook.yaml": webhook}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chart", type=Path, help="Use a local copy of the pinned official archive")
    parser.add_argument("--check", action="store_true", help="Check for drift in the committed files")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as scratch:
        archive = args.chart or Path(scratch) / "hami-2.11.0.tgz"
        if args.chart is None:
            with urllib.request.urlopen(CHART_URL, timeout=20) as response:
                archive.write_bytes(response.read())
        output = render(archive)
    for name, resources in output.items():
        body = HEADER + yaml.dump_all(resources, Dumper=ManifestDumper, sort_keys=False, explicit_start=True, width=120)
        if name.startswith("operator/"):
            body = "# Operator template. Supply webhooks[0].clientConfig.caBundle before applying.\n" + body
        path = DIRECTORY / name
        if args.check:
            if path.read_text() != body:
                raise SystemExit(f"Generated file differs from {name}")
        else:
            path.write_text(body)


if __name__ == "__main__":
    main()
