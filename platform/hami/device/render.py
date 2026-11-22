# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Project the reviewed HAMi device resources into their isolated namespace."""

import argparse
import hashlib
import json
from copy import deepcopy
import runpy
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import yaml

DIRECTORY = Path(__file__).resolve().parent
NAMESPACE = "hami-device"
RESOURCES = {
    ("ServiceAccount", "hami-device-plugin"),
    ("ConfigMap", "hami-device-plugin"),
    ("ConfigMap", "hami-scheduler-device"),
    ("Service", "hami-device-plugin-monitor"),
    ("DaemonSet", "hami-device-plugin"),
}
SELECTOR = {
    "airaware.training/gpu-mode": "hami",
    "gpu": "on",
    "airaware.training/pool": "training-hami",
    "airaware.training.node-restriction.kubernetes.io/gpu-mode": "hami",
}


class ManifestDumper(yaml.SafeDumper):
    def represent_str(self, value):
        return self.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ManifestDumper.add_representer(str, ManifestDumper.represent_str)


def device_daemonset(archive):
    chart_pin = runpy.run_path(str(DIRECTORY.parent / "system/render.py"))
    if archive is None:
        with tempfile.TemporaryDirectory(prefix="hami-device-chart-") as scratch:
            archive = Path(scratch) / "hami.tgz"
            with urllib.request.urlopen(chart_pin["CHART_URL"], timeout=20) as response:
                archive.write_bytes(response.read())
            return device_daemonset(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != chart_pin["CHART_SHA256"]:
        raise ValueError("HAMi chart archive SHA-256 differs from the system pin")
    result = subprocess.check_output([
        "helm", "template", "hami", str(archive), "--namespace", "hami-system",
        "--kube-version", "1.36.3", "-f", str(DIRECTORY.parent / "system/values.yaml"),
        "-f", str(DIRECTORY / "values.yaml"),
    ])
    daemonset, = (obj for obj in yaml.safe_load_all(result) if obj["kind"] == "DaemonSet")
    spec = daemonset["spec"]["template"]["spec"]
    del spec["priorityClassName"]
    if spec["nodeSelector"] != SELECTOR:
        raise ValueError("Unexpected dedicated device selector")
    daemonset["metadata"]["namespace"] = NAMESPACE
    return daemonset


def render(archive):
    source = DIRECTORY.parent / "system/core.yaml"
    selected = {}
    for obj in yaml.safe_load_all(source.read_text()):
        metadata = obj["metadata"]
        identity = (obj["kind"], metadata["name"])
        if identity not in RESOURCES:
            continue
        if identity in selected or metadata["namespace"] != "hami-system":
            raise ValueError(f"Unexpected upstream resource identity: {identity}")
        metadata["namespace"] = NAMESPACE
        if obj["kind"] == "DaemonSet":
            obj = device_daemonset(archive)
        selected[identity] = obj
    if selected.keys() != RESOURCES:
        raise ValueError("The reviewed device resource inventory differs from the upstream bundle")
    return "# Generated from the reviewed HAMi system core by render.py.\n" + yaml.dump_all(
        selected.values(), Dumper=ManifestDumper, sort_keys=False, explicit_start=True, width=120,
    )


def defaulted_spec(daemonset):
    """Account for Kubernetes 1.36 API defaults before admission compares the spec."""
    spec = deepcopy(daemonset["spec"])
    spec.setdefault("revisionHistoryLimit", 10)
    spec["updateStrategy"]["rollingUpdate"].setdefault("maxSurge", 0)
    pod = spec["template"]["spec"]
    pod.update({"restartPolicy": "Always", "schedulerName": "default-scheduler", "securityContext": {},
                "terminationGracePeriodSeconds": 30, "dnsPolicy": "ClusterFirst",
                "serviceAccount": pod["serviceAccountName"]})
    if pod.get("hostNetwork") is False:
        del pod["hostNetwork"]
    for volume in pod["volumes"]:
        if "hostPath" in volume:
            volume["hostPath"].setdefault("type", "")
        if "configMap" in volume:
            volume["configMap"].setdefault("defaultMode", 420)
    for container in pod["containers"] + pod["initContainers"]:
        container.setdefault("resources", {})
        container.setdefault("terminationMessagePolicy", "File")
        container.setdefault("terminationMessagePath", "/dev/termination-log")
        for env in container.get("env", []):
            field = env.get("valueFrom", {}).get("fieldRef")
            if field:
                field.setdefault("apiVersion", "v1")
        for name in ("readinessProbe", "livenessProbe", "startupProbe"):
            if name in container:
                probe = container[name]
                for key, value in {"failureThreshold": 3, "successThreshold": 1, "timeoutSeconds": 1}.items():
                    probe.setdefault(key, value)
                if "httpGet" in probe:
                    probe["httpGet"].setdefault("scheme", "HTTP")
    return spec


class CEL(str):
    """An unquoted expression in an otherwise literal map."""


def cel(value):
    if isinstance(value, CEL):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",\n".join(f"{json.dumps(k)}: dyn({cel(v)})" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",\n".join(f"dyn({cel(v)})" for v in value) + "]"
    return json.dumps(value)


def expected_pod(spec):
    pod = deepcopy(spec["template"]["spec"])
    token_name = CEL("variables.tokenVolumeName")
    pod.update({"enableServiceLinks": True, "preemptionPolicy": "PreemptLowerPriority", "priority": 0})
    pod["affinity"] = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {
        "nodeSelectorTerms": [{"matchFields": [{
            "key": "metadata.name", "operator": "In",
            "values": [CEL("variables.nodeTerms[0].matchFields[0].values[0]")],
        }]}],
    }}}
    for key in ("not-ready", "unreachable", "disk-pressure", "memory-pressure", "pid-pressure", "unschedulable"):
        pod["tolerations"].append({"key": f"node.kubernetes.io/{key}", "operator": "Exists",
                                   "effect": "NoExecute" if key in ("not-ready", "unreachable") else "NoSchedule"})
    pod["volumes"].append({
        "name": token_name, "projected": {"defaultMode": 420, "sources": [
            {"serviceAccountToken": {"expirationSeconds": 3607, "path": "token"}},
            {"configMap": {"items": [{"key": "ca.crt", "path": "ca.crt"}], "name": "kube-root-ca.crt"}},
            {"downwardAPI": {"items": [{"fieldRef": {"apiVersion": "v1", "fieldPath": "metadata.namespace"},
                                        "path": "namespace"}]}},
        ]},
    })
    for container in pod["containers"] + pod["initContainers"]:
        container["volumeMounts"].append({"name": token_name, "readOnly": True,
                                          "mountPath": "/var/run/secrets/kubernetes.io/serviceaccount"})
    return pod


def template_policy(name, group, resources, operations, validations, variables=()):
    api = "admissionregistration.k8s.io/v1"
    policy = {
        "apiVersion": api, "kind": "ValidatingAdmissionPolicy", "metadata": {"name": name},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {"resourceRules": [{"apiGroups": [group], "apiVersions": ["v1"],
                                                    "operations": operations, "resources": resources}]},
            "matchConditions": [{"name": "device-namespace", "expression": "request.namespace == 'hami-device'"}],
            "validations": [{"expression": expression, "message": message} for expression, message in validations],
        },
    }
    if variables:
        policy["spec"]["variables"] = [{"name": name, "expression": expression} for name, expression in variables]
    binding = {"apiVersion": api, "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": name},
               "spec": {"policyName": name, "validationActions": ["Deny"]}}
    return [policy, binding]


def render_guard(daemonset):
    spec = defaulted_spec(daemonset)
    pods = template_policy(
        "hami-device-pods", "", ["pods", "pods/ephemeralcontainers", "pods/resize"], ["CREATE", "UPDATE"],
        [
            ("""request.operation != 'CREATE'
                || (request.userInfo.username == 'system:serviceaccount:kube-system:daemon-set-controller'
                && has(object.metadata.ownerReferences) && size(object.metadata.ownerReferences) == 1
                && object.metadata.ownerReferences.exists(o, o.apiVersion == 'apps/v1' && o.kind == 'DaemonSet'
                && o.name == 'hami-device-plugin' && has(o.controller) && o.controller)
                && !has(object.spec.nodeName))""",
             "Only the reviewed DaemonSet controller may create device Pods, with assignment left to the scheduler."),
            ("""request.operation != 'CREATE' || (has(object.spec.affinity)
                && has(object.spec.affinity.nodeAffinity)
                && has(object.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution)
                && size(variables.nodeTerms) == 1
                && !has(variables.nodeTerms[0].matchExpressions)
                && has(variables.nodeTerms[0].matchFields)
                && size(variables.nodeTerms[0].matchFields) == 1
                && variables.nodeTerms[0].matchFields[0].key == 'metadata.name'
                && variables.nodeTerms[0].matchFields[0].operator == 'In'
                && size(variables.nodeTerms[0].matchFields[0].values) == 1)""",
             "The DaemonSet controller must target exactly one node using its normal affinity mutation."),
            ("request.operation != 'CREATE' || (variables.tokenVolumeName.matches('^kube-api-access-[a-z0-9]{5}$')"
             " && dyn(object.spec) == " + cel(expected_pod(spec)) + ")",
             "Device Pods must use the exact reviewed template and the default projected API credentials."),
            ("request.operation != 'UPDATE' || (object.spec == oldObject.spec"
             " && object.metadata.ownerReferences == oldObject.metadata.ownerReferences)",
             "The reviewed device Pod specification and owner cannot be modified."),
        ],
        [
            ("nodeTerms", "object.spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms"),
            ("tokenVolumeName", "object.spec.volumes[size(object.spec.volumes) - 1].name"),
        ],
    )
    daemonsets = template_policy(
        "hami-device-daemonset", "apps", ["daemonsets"], ["CREATE", "UPDATE"],
        [("object.metadata.name == 'hami-device-plugin' && dyn(object.spec) == " + cel(spec),
          "Only the exact reviewed device DaemonSet is permitted.")],
    )
    return "# Generated from the HAMi device core by render.py.\n" + yaml.dump_all(
        daemonsets + pods, Dumper=ManifestDumper, sort_keys=False, explicit_start=True, width=120,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check for drift in the committed device core")
    parser.add_argument("--chart", type=Path, help="Use a local copy of the pinned chart archive")
    options = parser.parse_args()
    body = render(options.chart)
    daemonset, = (obj for obj in yaml.safe_load_all(body) if obj["kind"] == "DaemonSet")
    outputs = {"core.yaml": body, "operator/template-admission.yaml": render_guard(daemonset)}
    for name, output in outputs.items():
        path = DIRECTORY / name
        if options.check:
            if path.read_text() != output:
                raise SystemExit(f"Generated file differs from {name}")
        else:
            path.write_text(output)
