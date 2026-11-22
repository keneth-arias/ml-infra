"""Render or verify the read-only description of the training PVC admission hold."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import uuid


API = "admissionregistration.k8s.io/v1"
NAME = "public-flamingo-training-claim"
NAMESPACE = "training"
CLAIM = "airaware-checkpoints"
CONTROLLER = "system:serviceaccount:kube-system:job-controller"
TRACKING = "batch.kubernetes.io/job-tracking"


def cel(value):
    """Give each heterogeneous list or map element a CEL dynamic type."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":dyn(" + cel(value[k]) + ")" for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join("dyn(" + cel(item) + ")" for item in value) + "]"
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def expected_spec(job):
    spec = deepcopy(job["spec"]["template"]["spec"])
    spec.setdefault("dnsPolicy", "ClusterFirst")
    spec.setdefault("schedulerName", "default-scheduler")
    spec.setdefault("terminationGracePeriodSeconds", 30)
    spec["serviceAccount"] = spec["serviceAccountName"]
    spec.setdefault("priority", 0)
    spec.setdefault("preemptionPolicy", "PreemptLowerPriority")
    tolerations = spec.setdefault("tolerations", [])
    for condition in ("not-ready", "unreachable"):
        key = "node.kubernetes.io/" + condition
        default = {"key": key, "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}
        if default not in tolerations:
            tolerations.append(default)
    for container in spec["containers"]:
        container.setdefault("terminationMessagePath", "/dev/termination-log")
        container.setdefault("terminationMessagePolicy", "File")
        container.setdefault("imagePullPolicy", "IfNotPresent")
    return spec


def has_claim(obj):
    return (f"has({obj}.spec.volumes) && {obj}.spec.volumes.exists(v, has(v.persistentVolumeClaim) && "
            f"v.persistentVolumeClaim.claimName == '{CLAIM}')")


def validation(expression):
    return {"expression": expression, "message": "Training claim requires the reviewed exclusive writer."}


def same_metadata(field):
    return (f"has(object.metadata.{field}) == has(oldObject.metadata.{field}) && "
            f"(!has(object.metadata.{field}) || object.metadata.{field} == oldObject.metadata.{field})")


def render_guard(run, phase, pins=None, job=None):
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", run) or phase not in {"stage", "owner", "closed"}:
        raise ValueError("Expected a valid run and guard phase")
    namespace_selector = {"matchLabels": {"kubernetes.io/metadata.name": NAMESPACE}}
    match = {"matchPolicy": "Exact", "namespaceSelector": namespace_selector, "objectSelector": {},
             "resourceRules": [{"apiGroups": [""], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
                                "resources": ["pods", "pods/ephemeralcontainers", "pods/resize"], "scope": "Namespaced"}]}
    rules = ["!has(request.subResource) || request.subResource == ''", f"request.userInfo.username == '{CONTROLLER}'"]
    # After scheduling, nodeName differs from the creation template. On UPDATE permit
    # only the Job controller releasing its accounting finalizer on an unchanged Pod.
    finalizing = ("request.operation == 'UPDATE' && object.spec == oldObject.spec && "
                  "object.metadata.uid == oldObject.metadata.uid && "
                  f"has(oldObject.metadata.finalizers) && oldObject.metadata.finalizers == ['{TRACKING}'] && "
                  "(!has(object.metadata.finalizers) || size(object.metadata.finalizers) == 0) && "
                  + " && ".join("(" + same_metadata(k) + ")" for k in ("ownerReferences", "labels", "annotations")))
    rules.append(finalizing if phase == "closed" else "request.operation == 'CREATE' || (" + finalizing + ")")
    if phase != "closed":
        if not job or not pins:
            raise ValueError("Active guard requires a reviewed Job and its enrolled UID")
        uid = str(uuid.UUID(pins["job_uid"]))
        job_name = f"stage-{run}" + ("-owner" if phase == "owner" else "")
        if ((job.get("apiVersion"), job.get("kind"), job["metadata"].get("namespace"), job["metadata"]["name"])
                != ("batch/v1", "Job", NAMESPACE, job_name)):
            raise ValueError("Wrong reviewed Job or namespace")
        labels = dict(job["spec"]["template"].get("metadata", {}).get("labels", {}))
        labels.update({"batch.kubernetes.io/job-name": job_name, "job-name": job_name,
                       "batch.kubernetes.io/controller-uid": uid, "controller-uid": uid})
        regional = {**labels, "topology.kubernetes.io/region": "eu-south1"}
        owner = {"apiVersion": "batch/v1", "kind": "Job", "controller": True, "blockOwnerDeletion": True,
                 "name": job_name, "uid": uid}
        checks = [
            f"object.metadata.generateName == {cel(job_name + '-')}",
            "has(object.metadata.ownerReferences) && dyn(object.metadata.ownerReferences) == dyn(" + cel([owner]) + ")",
            "has(object.metadata.labels) && (dyn(object.metadata.labels) == dyn(" + cel(labels) + ") || "
            "dyn(object.metadata.labels) == dyn(" + cel(regional) + "))",
            "!has(object.metadata.annotations) || size(object.metadata.annotations) == 0",
            f"has(object.metadata.finalizers) && object.metadata.finalizers == ['{TRACKING}']",
            "dyn(object.spec) == dyn(" + cel(expected_spec(job)) + ")",
        ]
        rules.extend("request.operation != 'CREATE' || (" + check + ")" for check in checks)

    policy = {"apiVersion": API, "kind": "ValidatingAdmissionPolicy", "metadata": {"name": NAME},
              "spec": {"failurePolicy": "Fail", "matchConstraints": match, "matchConditions": [
                  {"name": "protected-claim", "expression": "(" + has_claim("object") + ") || "
                   "(oldObject != null && " + has_claim("oldObject") + ")"}],
                       "validations": [validation(rule) for rule in rules]}}
    binding = {"apiVersion": API, "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": NAME},
               "spec": {"policyName": NAME, "validationActions": ["Deny"],
                        "matchResources": {"matchPolicy": "Exact", "namespaceSelector": namespace_selector,
                                           "objectSelector": {}}}}
    return policy, binding


def verify_guard(get, run, phase, pins, job=None):
    """Verify independently enrolled UIDs and generations plus every effective rule."""
    reviewed = render_guard(run, phase, pins, job)
    observed = {}
    for role, expected in zip(("policy", "binding"), reviewed):
        kind = expected["kind"]
        actual = get(kind.lower(), NAME)
        metadata = actual["metadata"]
        if (actual.get("kind") != kind or actual.get("apiVersion") != API or metadata.get("name") != NAME
                or metadata.get("deletionTimestamp") or actual.get("spec") != expected["spec"]):
            raise ValueError("Admission guard differs from the reviewed policy or binding")
        uid = str(uuid.UUID(pins[role + "_uid"]))
        generation = pins[role + "_generation"]
        if (type(generation) is not int or generation < 1 or metadata.get("uid") != uid
                or metadata.get("generation") != generation):
            raise ValueError("Admission guard UID or generation differs from approval")
        if role == "policy":
            status = actual.get("status", {})
            checking = status.get("typeChecking")
            if (status.get("observedGeneration") != generation or not isinstance(checking, dict)
                    or checking.get("expressionWarnings")):
                raise ValueError("Admission guard lacks current successful CEL type checking")
        observed.update({role + "_uid": uid, role + "_generation": generation})
    if phase != "closed":
        name = job["metadata"]["name"]
        live = get("job", name, "-n", NAMESPACE)
        if (live.get("apiVersion") != "batch/v1" or live.get("kind") != "Job"
                or live["metadata"].get("namespace") != NAMESPACE or live["metadata"].get("name") != name
                or live["metadata"].get("deletionTimestamp") or live["metadata"].get("uid") != pins["job_uid"]):
            raise ValueError("Active guard does not identify the enrolled Job")
        observed["job_uid"] = pins["job_uid"]
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--phase", required=True, choices=("closed", "owner", "stage"))
    parser.add_argument("--job", type=Path, help="Reviewed Job JSON for the active phase")
    parser.add_argument("--job-uid", help="Independently observed suspended Job UID")
    args = parser.parse_args()
    job = json.loads(args.job.read_bytes()) if args.job else None
    items = render_guard(args.run, args.phase, {"job_uid": args.job_uid} if args.job_uid else None, job)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
