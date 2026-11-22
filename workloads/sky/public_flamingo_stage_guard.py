"""Render or verify the read-only description of the training PVC admission hold."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import runpy
import uuid

import public_flamingo_window as windows


API = "admissionregistration.k8s.io/v1"
NAME = "public-flamingo-training-claim"
NAMESPACE = "training"
CLAIM = "airaware-checkpoints"
CONTROLLER = "system:serviceaccount:kube-system:job-controller"
TRACKING = "batch.kubernetes.io/job-tracking"
# The public-baseline release is scoped to the original support-20261121d GPU allocation.
ALLOCATION_CUTOFF = datetime(2026, 11, 22, 2, 25, tzinfo=timezone.utc)
CLOSEOUT_SECONDS = 120
CONTAINER_FIELDS = ("containers", "initContainers", "ephemeralContainers")
# The same local Secret references must be recognized before issuance and at Pod admission.
VOLUME_SECRET_FIELDS = (
    ("secret", "secretName"), ("azureFile", "secretName"), ("csi", "nodePublishSecretRef", "name"),
    *((kind, "secretRef", "name") for kind in ("cephfs", "cinder", "flexVolume", "rbd", "scaleIO", "iscsi", "storageos")),
)


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


def preexisting_secret_consumer(pod, name):
    """Return whether this existing Pod can consume the named local Secret, even without the PVC."""
    spec = pod["spec"]
    if any(ref.get("name") == name for ref in spec.get("imagePullSecrets", [])):
        return True
    for volume in spec.get("volumes", []):
        for fields in VOLUME_SECRET_FIELDS:
            value = volume
            for field in fields:
                value = value.get(field, {})
            if value == name:
                return True
        if any(src.get("secret", {}).get("name") == name for src in volume.get("projected", {}).get("sources", [])):
            return True
    for field in CONTAINER_FIELDS:
        for container in spec.get(field, []):
            if any(src.get("secretRef", {}).get("name") == name for src in container.get("envFrom", [])):
                return True
            if any(env.get("valueFrom", {}).get("secretKeyRef", {}).get("name") == name
                   for env in container.get("env", [])):
                return True
    return False


def protected_inputs(obj, inventory):
    """Mirror core/v1 VisitPodSecretNames for direct Secret references on a Pod."""
    spec = obj + ".spec"
    secret = cel(inventory)
    sources = [f"has(v.persistentVolumeClaim) && v.persistentVolumeClaim.claimName == '{CLAIM}'",
               "has(v.projected) && has(v.projected.sources) && v.projected.sources.exists(p, "
               f"has(p.secret) && has(p.secret.name) && p.secret.name == {secret})"]
    for fields in VOLUME_SECRET_FIELDS:
        path = "v"
        checks = []
        for field in fields:
            path += "." + field
            checks.append(f"has({path})")
        sources.append(" && ".join(checks + [f"{path} == {secret}"]))
    volume_ref = " || ".join("(" + source + ")" for source in sources)
    tests = [f"has({spec}.volumes) && {spec}.volumes.exists(v, {volume_ref})",
             f"has({spec}.imagePullSecrets) && {spec}.imagePullSecrets.exists(p, has(p.name) && p.name == {secret})"]
    env = ("has(c.env) && c.env.exists(e, has(e.valueFrom) && has(e.valueFrom.secretKeyRef) && "
           f"has(e.valueFrom.secretKeyRef.name) && e.valueFrom.secretKeyRef.name == {secret})")
    env_from = ("has(c.envFrom) && c.envFrom.exists(e, has(e.secretRef) && has(e.secretRef.name) "
                f"&& e.secretRef.name == {secret})")
    for field in CONTAINER_FIELDS:
        path = f"{spec}.{field}"
        tests.append(f"has({path}) && {path}.exists(c, ({env}) || ({env_from}))")
    return " || ".join("(" + test + ")" for test in tests)


def latest_start(expires_at, job, window=None):
    end = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if end.tzinfo is None:
        raise ValueError("Expected an independently approved expiry with UTC offset")
    deadline = job["spec"]["activeDeadlineSeconds"]
    grace = job["spec"]["template"]["spec"].get("terminationGracePeriodSeconds", 30)
    if type(deadline) is not int or not 0 < deadline <= 1800 or type(grace) is not int or not 0 <= grace < CLOSEOUT_SECONDS:
        raise ValueError("Job deadline or termination grace exceeds the fixed handoff budget")
    cutoff = datetime.fromisoformat(window.cutoff.replace("Z", "+00:00")) if window else ALLOCATION_CUTOFF
    latest = min(end.astimezone(timezone.utc), cutoff) - timedelta(seconds=deadline + CLOSEOUT_SECONDS)
    return latest.isoformat(timespec="seconds").replace("+00:00", "Z")


def validation(expression):
    return {"expression": expression, "message": "Training claim requires the reviewed exclusive writer."}


def same_metadata(field):
    return (f"has(object.metadata.{field}) == has(oldObject.metadata.{field}) && "
            f"(!has(object.metadata.{field}) || object.metadata.{field} == oldObject.metadata.{field})")


def reviewed_window_job(window, run, phase):
    here = Path(__file__).resolve().parent
    stage = runpy.run_path(str(here / "render-public-flamingo-stage.py"))
    stage_job = stage["stage_job"](run, "stage-" + run + "-inventory", "stage-" + run + "-program",
                                   window.preflight_image, sha256(stage["WORKER"].read_bytes()).hexdigest(),
                                   window.objects_sha256, window)
    if phase == "stage":
        return stage_job
    owner = runpy.run_path(str(here / "prepare-public-flamingo-leaves.py"))
    return owner["owner_job"](stage_job, owner["worker_source"]())


def render_guard(run, phase, pins=None, job=None, window=None):
    windows.validate_window_run(window, run)
    windows.validate_window_pin(window, pins or {})

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
        if window and job != reviewed_window_job(window, run, phase):
            raise ValueError("Job differs from the independently reproduced window and role")
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
        latest = latest_start(pins["expires_at"], job, window)
        checks = [
            "timestamp(dyn(object.metadata).creationTimestamp) < timestamp(" + cel(latest) + ")",
            f"object.metadata.generateName == {cel(job_name + '-')}",
            "has(object.metadata.ownerReferences) && dyn(object.metadata.ownerReferences) == dyn(" + cel([owner]) + ")",
            "has(object.metadata.labels) && (dyn(object.metadata.labels) == dyn(" + cel(labels) + ") || "
            "dyn(object.metadata.labels) == dyn(" + cel(regional) + "))",
            "!has(object.metadata.annotations) || size(object.metadata.annotations) == 0",
            f"has(object.metadata.finalizers) && object.metadata.finalizers == ['{TRACKING}']",
            "dyn(object.spec) == dyn(" + cel(expected_spec(job)) + ")",
        ]
        if window:
            checks.insert(0, "timestamp(dyn(object.metadata).creationTimestamp) >= timestamp(" + cel(window.start) + ")")
        rules.extend("request.operation != 'CREATE' || (" + check + ")" for check in checks)

    inventory = f"stage-{run}-inventory"
    policy = {"apiVersion": API, "kind": "ValidatingAdmissionPolicy", "metadata": {"name": NAME},
              "spec": {"failurePolicy": "Fail", "matchConstraints": match, "matchConditions": [
                  {"name": "protected-inputs", "expression": "(" + protected_inputs("object", inventory) + ") || "
                   "(oldObject != null && (" + protected_inputs("oldObject", inventory) + "))"}],
                       "validations": [validation(rule) for rule in rules]}}
    binding = {"apiVersion": API, "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": NAME},
               "spec": {"policyName": NAME, "validationActions": ["Deny"],
                        "matchResources": {"matchPolicy": "Exact", "namespaceSelector": namespace_selector,
                                           "objectSelector": {}}}}
    if window:
        policy["spec"]["auditAnnotations"] = [{"key": "stage-window", "valueExpression": cel(window.sha256)}]
    return policy, binding


def verify_guard(get, run, phase, pins, job=None, window=None):
    """Verify independently enrolled UIDs and generations plus every effective rule."""
    reviewed = render_guard(run, phase, pins, job, window)
    observed = {}
    if window:
        windows.verify_live_window(window)
        window.validate_namespace(get("namespace", NAMESPACE))
        window.validate_placement(get("node", window.cpu_node))
        observed["window_sha256"] = window.sha256
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
                or live["metadata"].get("deletionTimestamp") or live["metadata"].get("uid") != pins["job_uid"]
                or (window and live["metadata"].get("annotations", {}).get("airaware.training/stage-window-sha256")
                    != window.sha256)):
            raise ValueError("Active guard does not identify the enrolled Job")
        observed.update(job_uid=pins["job_uid"], expires_at=pins["expires_at"])
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--phase", required=True, choices=("closed", "owner", "stage"))
    parser.add_argument("--job", type=Path, help="Reviewed Job JSON for the active phase")
    parser.add_argument("--job-uid", help="Independently observed suspended Job UID")
    parser.add_argument("--expires-at", help="Independent UTC-offset storage-owner approval expiry")
    windows.add_window_args(parser)
    args = parser.parse_args()
    window = windows.load_window(args, args.run, live=False)
    if args.phase != "closed" and not (args.job_uid and args.job and args.expires_at):
        parser.error("Opening the guard requires --job, --job-uid and --expires-at")
    job = json.loads(args.job.read_bytes()) if args.job else None
    pins = {"window_sha256": window.sha256} if window else {}
    if args.job_uid:
        pins.update(job_uid=args.job_uid, expires_at=args.expires_at)
    items = render_guard(args.run, args.phase, pins, job, window)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
