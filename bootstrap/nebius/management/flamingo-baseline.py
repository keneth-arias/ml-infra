"""Fund, enroll and release one pre-fit baseline Job on an existing managed worker."""
import argparse
import copy
import fcntl
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import signal
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
N = runpy.run_path(str(HERE / "native-activation.py"))
ROOT = N["ROOT"]
R = runpy.run_path(str(ROOT / "workloads/smoke/render-flamingo-baseline.py"))
STORE = runpy.run_path(str(ROOT / "workloads/sky/training-storage-handoff.py"))
render = R["render"]
from control_storage import LEDGER_KEY, control_bucket, verify_bucket_projects
from gpu_reservation import read_ledger, with_ledger_client, write_reservation
from gpu_window import bounds, check_projection, check_vm_funding
from production_release import current_release, check_native_policy
from production_target import kubectl_client_for_identity
from worker_cleanup import scope_digest
from worker_plan import check_gpu_mode, timestamp


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def private(path):
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f"Expected an owner-only file: {path}")
    return path.read_bytes()


def baseline_role(receipt, name):
    role = N["access_objects"](receipt)["items"][0]
    role["rules"] = [{"apiGroups": ["batch"], "resources": ["jobs"], "resourceNames": [name],
                      "verbs": ["get", "patch"]}]
    return role


def baseline_checks(job, uid):
    # Only an already enrolled, still-suspended UID can cross this gate. The
    # whole pod template (including scheduling directives mutable on suspend)
    # and every mutable execution setting must survive the update intact.
    expressions = [
        "request.operation == 'UPDATE' && object.metadata.name == " + repr(job["metadata"]["name"])
        + " && oldObject.metadata.uid == " + repr(str(uuid.UUID(uid))),
        "oldObject.metadata.generation == 1 && oldObject.spec.suspend && !object.spec.suspend",
        "object.spec.template == oldObject.spec.template && object.spec.selector == oldObject.spec.selector",
        "object.metadata.labels == oldObject.metadata.labels && !has(object.metadata.annotations)"
        " && !has(object.metadata.ownerReferences) && !has(object.metadata.finalizers)",
        "object.spec.parallelism == 1 && object.spec.completions == 1 && object.spec.backoffLimit == 0"
        + " && object.spec.activeDeadlineSeconds == " + str(job["spec"]["activeDeadlineSeconds"]),
        "!has(object.spec.ttlSecondsAfterFinished) && !has(object.spec.successPolicy)"
        " && !has(object.spec.podFailurePolicy) && !has(object.spec.backoffLimitPerIndex)"
        " && !has(object.spec.maxFailedIndexes) && !has(object.spec.managedBy)"
        " && (!has(object.spec.manualSelector) || !object.spec.manualSelector)"
        " && object.spec.completionMode == 'NonIndexed'"
        " && object.spec.podReplacementPolicy == 'TerminatingOrFailed'",
    ]
    return [{"expression": expr, "message": "Only the enrolled Flamingo baseline may be released."}
            for expr in expressions]


def verified_enrollment(job, observed):
    actual = copy.deepcopy(observed)
    meta = actual["metadata"]
    uid = str(uuid.UUID(meta["uid"]))
    if (actual["kind"] != "Job" or actual["apiVersion"] != "batch/v1"
            or meta["generation"] != (1 if job["spec"]["suspend"] else 2)
            or any(meta.get(k) for k in ("deletionTimestamp", "annotations", "ownerReferences", "finalizers"))
            or any(meta.get(k) != job["metadata"][k] for k in ("namespace", "name", "labels"))):
        raise ValueError("Enrolled Job identity or owner changed")
    spec = actual["spec"]
    pod_meta = spec["template"]["metadata"]
    for key in ("controller-uid", "batch.kubernetes.io/controller-uid", "job-name", "batch.kubernetes.io/job-name"):
        expected = uid if key.endswith("uid") else meta["name"]
        if pod_meta["labels"].pop(key, expected) != expected:
            raise ValueError("Unexpected controller labels")
    if spec.pop("selector") != {"matchLabels": {"batch.kubernetes.io/controller-uid": uid}}:
        raise ValueError("Enrolled Job selector changed")
    pod = spec["template"]["spec"]
    container, = pod["containers"]
    for obj, defaults in (
        (spec, {"completionMode": "NonIndexed", "manualSelector": False, "podReplacementPolicy": "TerminatingOrFailed"}),
        (pod_meta, {"creationTimestamp": None}),
        (pod, {"dnsPolicy": "ClusterFirst", "schedulerName": "default-scheduler",
               "enableServiceLinks": True, "serviceAccount": "episode-worker"}),
        (container, {"imagePullPolicy": "IfNotPresent", "terminationMessagePath": "/dev/termination-log",
                     "terminationMessagePolicy": "File"}),
    ):
        for key, default in defaults.items():
            if obj.pop(key, default) != default:
                raise ValueError(f"Unexpected Job default: {key}")
    if spec != job["spec"]:
        raise ValueError("Admitted Job differs from the exact reviewed enrollment")
    return uid


def baseline_request(args, scope, scope_hash, images, receipt):
    review = json.loads(private(args.review))
    job = json.loads(review["job_json"])
    pod = job["spec"]["template"]["spec"]
    node = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchFields"][0]["values"][0]
    vm = review["provider_id"].removeprefix("nebius://")
    cluster = os.environ["NEBIUS_TRAINING_CLUSTER_ID"]
    run = job["metadata"]["labels"]["airaware.training/run"]
    deadline = job["spec"]["activeDeadlineSeconds"]
    limit = int(pod["containers"][0]["env"][-1]["value"])
    expected = render(images["flamingo-trainer"], run, node, deadline, limit)
    if (review["schema"] != "flamingo-baseline-review-v1" or canonical(expected).decode() != review["job_json"]
            or sha(canonical(expected)) != review["job_sha256"] or type(deadline) != int
            or receipt != args.expected_deployment or review["deployment_sha256"] != receipt
            or scope["cluster_id"] != cluster or review["cluster_id"] != cluster or scope["stage"] != "gpu"
            or scope.get("gpu_mode", "native") != "native"
            or not re.fullmatch("computeinstance-[a-z0-9]+", vm) or review["provider_id"] != "nebius://" + vm
            or not re.fullmatch("[a-z0-9][a-z0-9-]{0,39}", scope["run_id"])):
        raise ValueError("Review differs from signed renderer, enrolled worker or managed allocation")
    staged = json.loads(private(args.staging))
    binding_hash = sha(private(args.binding))
    project, binding = STORE["approved_binding"](args.binding, binding_hash, cluster)
    expected_stage = {"cluster_id": cluster, "run": run, "disk_id": binding[2],
                      "weights_manifest_sha256": R["FLAMINGO_WEIGHTS"],
                      "output_subpath": "flamingo-baselines/" + run, "output_uid": 1000,
                      "output_empty": True, "writer_fenced": True}
    if project != scope["project_id"] or any(staged.get(k) != v for k, v in expected_stage.items()):
        raise ValueError("Staging receipt lacks verified weights, fresh output or fenced writer")
    request = {"purpose": "flamingo-baseline", "attempt_id": "flamingo-base-" + run,
               "cluster_id": cluster, "start": scope["start"], "cutoff": scope["cutoff"],
               "scope_sha256": scope_hash, "deployment_sha256": receipt,
               "job_sha256": review["job_sha256"], "node": node, "node_uid": str(uuid.UUID(args.node_uid)),
               "provider_id": review["provider_id"], "vm": vm, "binding_sha256": binding_hash,
               "staging_sha256": sha(canonical(staged)), "deadline_seconds": deadline,
               "termination_grace_seconds": pod["terminationGracePeriodSeconds"]}
    return request, expected, binding


def funded_limits(scope, ledger, request, now, margin):
    if check_gpu_mode(scope, ledger) != "native":
        raise ValueError("Flamingo baseline requires a native GPU window")
    if ([v["vm"] for v in ledger["gpu_intervals"] if v["end"] is None] != [request["vm"]]
            or request["cutoff"] != scope["cutoff"] or request["start"] != scope["start"]
            or request["cluster_id"] != scope["cluster_id"]):
        raise ValueError("Funding requires the exact current worker and allocation")
    limits = bounds(scope, ledger, now)
    check_vm_funding(scope, limits, now)
    seconds = request["deadline_seconds"] + request["termination_grace_seconds"] + margin
    check_projection(limits, Decimal(seconds) / 3600)
    return {k: str(v) for k, v in limits.items()}


def reserve(s3, scope, ledger_sha, version, request, now):
    current, ledger = read_ledger(s3, dict(scope, ledger_sha256=ledger_sha))
    if (current["VersionId"] != version or ledger.get("active_reservation")
            or ledger.get("baseline_approval") != request or request["attempt_id"] in ledger.get("attempt_ids", [])):
        raise ValueError("Require the exact unconsumed, approved off-host funding version")
    limits = funded_limits(scope, ledger, request, now, 120)
    hold = {"stage": "flamingo-baseline", "attempt_id": request["attempt_id"], "run_id": scope["run_id"],
            "vm": request["vm"], "request": request, "prior_version": version,
            "prior_revision": ledger["revision"], "limits": limits, "reserved_at": now.isoformat()}
    revision, new_version = write_reservation(s3, scope, current, ledger, hold)
    return {"revision": revision, "version_id": new_version, "ledger_sha256": sha(json.dumps(ledger, sort_keys=True).encode()),
            "bucket": control_bucket(scope), "key": LEDGER_KEY, "hold": hold}


def check_reserved(s3, scope, receipt, now):
    current, ledger = read_ledger(s3, dict(scope, ledger_sha256=receipt["ledger_sha256"]))
    if (current["VersionId"] != receipt["version_id"] or ledger["revision"] != receipt["revision"]
            or ledger["active_reservation"] != receipt["hold"]):
        raise ValueError("Baseline no longer owns the exact live funding hold")
    funded_limits(scope, ledger, receipt["hold"]["request"], now, 30)


def record_submission(s3, scope, submitted, now):
    # Keep the authoritative hold active. One conditional ledger successor
    # associates the Kubernetes UID with the exact funded attempt forever in
    # version history. Retrying the receipt publication cannot issue a Job.
    funding = submitted["funding"]
    obj = s3.get_object(Bucket=control_bucket(scope), Key=LEDGER_KEY)
    raw = obj["Body"].read()
    ledger = json.loads(raw)
    version = obj["VersionId"]
    if not version or version == "null" or ledger["active_reservation"] != funding["hold"]:
        raise ValueError("Off-host hold has changed; reconcile its version history")
    record = {k: submitted[k] for k in ("job", "namespace", "uid")}
    record.update(funding_version=funding["version_id"], request=funding["hold"]["request"])
    if ledger.get("baseline_submission") != record:
        if version != funding["version_id"] or sha(raw) != funding["ledger_sha256"]:
            raise ValueError("Another funding revision intervened before UID publication")
        ledger.update(baseline_submission=record, revision=uuid.uuid4().hex)
        raw = json.dumps(ledger, sort_keys=True).encode()
        version = s3.put_object(Bucket=control_bucket(scope), Key=LEDGER_KEY, IfMatch=obj["ETag"],
                                Body=raw, ContentType="application/json")["VersionId"]
        if not version or version == "null":
            raise ValueError("Unversioned submission receipt; reconcile the off-host ledger")
    return {"bucket": control_bucket(scope), "key": LEDGER_KEY, "version_id": version,
            "ledger_sha256": sha(raw), **record}


def output(*cmd):
    return subprocess.check_output(cmd, text=True, timeout=20).strip()


def check_schedulers(scope, scope_hash):
    run = scope["run_id"]
    timer = "worker-expiry@" + run + ".timer"
    if (output("systemctl", "is-active", timer) != "active" or output("systemctl", "is-enabled", timer) != "enabled"
            or output("sudo", "-n", "sha256sum", "/etc/nebius-cleanup/" + run + "-scope.json").split()[0] != scope_hash):
        raise ValueError("bm cutoff timer or installed allocation scope changed")
    fields = dict(line.split("=", 1) for line in
                  output("sudo", "-n", "cat", "/etc/nebius-cleanup/" + run + ".env").splitlines())
    if any(fields.get(key) != scope[field] for key, field in
           (("CLUSTER_ID", "cluster_id"), ("PROJECT_ID", "project_id"), ("CUTOFF", "cutoff"))):
        raise ValueError("bm expiry service differs from the enrolled target or cutoff")
    remote = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8",
              "fw13", "kubectl", "--context", "kind-kind-cluster", "-n", "nebius-cleanup", "--request-timeout=10s"]
    cron = json.loads(output(*remote, "get", "cronjob", "expiry-" + run, "-o", "json"))
    meta, spec = cron["metadata"], cron["spec"]
    cm_name = "expiry-" + run + "-gpu"
    cm = json.loads(output(*remote, "get", "configmap", cm_name, "-o", "json"))
    if (cm.get("immutable") is not True or cm["metadata"].get("deletionTimestamp")
            or sha(cm["data"]["scope.json"].encode()) != scope_hash
            or not any(v["name"] == "scope" and v.get("configMap", {}).get("name") == cm_name
                       for v in spec["jobTemplate"]["spec"]["template"]["spec"]["volumes"])):
        raise ValueError("fw13 expiry scope changed")
    next_tick = output("systemctl", "show", timer, "-p", "NextElapseUSecRealtime", "--value")
    next_epoch = int(output("date", "-u", "-d", next_tick, "+%s"))
    now = N["utcnow"]()
    record = {"uid": meta["uid"], "spec_sha256": scope_digest(spec),
              "image": spec["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"]}
    if (meta.get("deletionTimestamp") or spec["suspend"] or record != scope["fw13_cronjob"]
            or not now - timedelta(seconds=90) <= timestamp(cron["status"]["lastSuccessfulTime"]) <= now
            or not 0 < next_epoch - now.timestamp() <= 65):
        raise ValueError("Expected two armed cutoff schedulers with current ticks")
    return {"fw13": record, "last_success": cron["status"]["lastSuccessfulTime"], "bm_next_tick": next_tick}


def verify_placement(client, request, binding, project):
    def get(*args):
        return json.loads(output(*client, "--request-timeout=12s", "get", *args, "-o", "json"))

    node = get("node", request["node"])
    meta, spec, status = node["metadata"], node["spec"], node["status"]
    if (meta["uid"] != request["node_uid"] or meta["name"] != request["node"] or meta.get("deletionTimestamp")
            or spec["providerID"] != request["provider_id"] or spec.get("unschedulable")
            or meta["labels"].get("airaware.training/pool") != "training-gpu"
            or status["allocatable"].get("nvidia.com/gpu") != "1"
            or not any(c["type"] == "Ready" and c["status"] == "True" for c in status["conditions"])):
        raise ValueError("Funded worker identity or readiness changed")
    instance = json.loads(output("nebius", "-p", "ml-prod", "--no-browser", "--no-check-update", "--no-progress",
                                 "--format", "json", "compute", "instance", "get", request["vm"]))
    if ((instance["metadata"]["id"], instance["metadata"]["parent_id"]) != (request["vm"], project)
            or instance["status"]["state"] != "RUNNING" or instance["status"].get("reconciling")):
        raise ValueError("Funded provider VM is not running in the approved project")
    observed, idle = STORE["status"](get)
    if observed != binding or not idle:
        raise ValueError("Funded training storage is in use or its binding changed")
    disk = json.loads(output("nebius", "-p", "ml-prod", "--no-browser", "--no-check-update", "--no-progress",
                             "--format", "json", "compute", "disk", "get", binding[2]))
    if not STORE["provider_detached"](disk, project, binding[2]):
        raise ValueError("Enrolled disk is not detached at the provider")


def session_intent(target, identity, manager, receipt, images, held, guard, job, uid):
    attempt = uuid.uuid4().hex
    policy = copy.deepcopy(held)
    check_native_policy(images, policy)
    policy["metadata"]["annotations"] = {N["ATTEMPT"]: attempt, N["RECEIPT"]: receipt, N["STAGE"]: "flamingo-baseline"}
    policy["spec"].pop("variables")
    policy["spec"]["validations"] = baseline_checks(job, uid)
    role = baseline_role(receipt, job["metadata"]["name"])
    binding = N["access_objects"](receipt)["items"][1]
    for obj in (role, binding):
        obj["metadata"]["labels"] = {N["ATTEMPT"]: attempt}
    marker = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
              "metadata": {"name": "episode-native-" + attempt, "namespace": "training",
                           "labels": {N["ATTEMPT"]: attempt}}}
    return {"attempt": attempt, "target": target, "operator": identity, "manager": manager,
            "receipt": receipt, "marker": marker, "role": role, "binding": binding, "guard": guard,
            "held": held, "active": policy, "job": job["metadata"]["name"], "job_uid": uid}


class BaselineSession(N["Transaction"]):
    def patch(self, changes, *, dry=False):
        client = ["kubectl", "--kubeconfig", str(self.session / "kubeconfig"), "--context", "prod-nebius",
                  "--request-timeout=10s", "-n", "training"]
        return json.loads(self.call("patch", "job", self.intent["job"], "--type=json", "--patch-file=/dev/stdin",
                                    *(["--dry-run=server"] if dry else []), "-o", "json", client=client, payload=changes))

    def probe(self, images=None):
        if images is None:
            return super().probe()
        env = dict(os.environ, NEBIUS_SUBMIT_KUBECONFIG=str(self.session / "kubeconfig"))
        # Reuse the held checks: global permissions must remain closed. Only
        # get/patch for this exact named Job can be granted.
        subprocess.run([sys.executable, str(ROOT / "workloads/smoke/verify-training-access.py")],
                       env=env, check=True, capture_output=True, timeout=80)
        changes = self.release_patch()
        reply = self.patch(changes, dry=True)
        if reply["metadata"]["uid"] != self.intent["job_uid"] or reply["spec"]["suspend"] is not False:
            raise ValueError("Scoped dry run did not release the enrolled UID")
        try:
            self.patch(changes + [{"op": "replace", "path": "/spec/parallelism", "value": 0}], dry=True)
        except subprocess.CalledProcessError as exc:
            if N["NAME"] not in exc.stderr or "Only the enrolled Flamingo baseline" not in exc.stderr:
                raise
        else:
            raise ValueError("Baseline policy admitted an unfunded workload mutation")

    def release_patch(self):
        obj = self.live("jobs", self.intent["job"])
        if verified_enrollment(self.checkpoint_value("enrollment"), obj) != self.intent["job_uid"]:
            raise ValueError("Enrolled Job UID changed")
        return [{"op": "test", "path": "/metadata/uid", "value": self.intent["job_uid"]},
                {"op": "test", "path": "/metadata/resourceVersion", "value": obj["metadata"]["resourceVersion"]},
                {"op": "test", "path": "/spec/suspend", "value": True},
                {"op": "replace", "path": "/spec/suspend", "value": False}]


def submit(args, scope, request, job, binding, images, deployment):
    identity = os.environ["NEBIUS_STAGING_OPERATOR_IDENTITY"]
    if not identity.strip() or identity.startswith("system:") or not args.field_manager.strip():
        raise ValueError("Expected independent managed operator and approved field manager")
    operator = Path(os.environ["NEBIUS_SUBMIT_KUBECONFIG"])
    private(operator)
    client = kubectl_client_for_identity("training", identity, direct=True)
    target = {"cluster_id": request["cluster_id"], "endpoint": os.environ["NEBIUS_API_ENDPOINT"],
              "ca_sha256": os.environ["NEBIUS_CA_BUNDLE_SHA256"]}
    ca = N["pinned_kubeconfig_ca"](client, target["cluster_id"], target["ca_sha256"], endpoint=target["endpoint"], direct=True)
    cutoff = N["require_window"](args.scope, "gpu", target["cluster_id"], N["utcnow"]())
    verify_bucket_projects(scope)
    held, guard = N["held_policy_and_binding"]()
    check_native_policy(images, held)
    args.session.mkdir(mode=0o700)
    with (args.session / "lock").open("x") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tx = BaselineSession(client, ca, target, args.session, {})
        tx.checkpoint("request", request)
        N["check_active"](held, guard, tx.policy_list())
        verify_placement(client, request, binding, scope["project_id"])
        tx.checkpoint("schedulers", check_schedulers(scope, request["scope_sha256"]))
        name = job["metadata"]["name"]
        if tx.live("jobs", name) is not None:
            raise ValueError("Baseline name already exists; reconcile the original enrollment")
        enrolled = copy.deepcopy(job)
        enrolled["spec"]["suspend"] = True
        create = ["create", "--validate=strict", "-f", "-", "-o", "json"]
        trial = json.loads(tx.call(*create, "--dry-run=server", payload=enrolled))
        verified_enrollment(enrolled, trial)
        tx.checkpoint("funding-intent", {"version_id": args.ledger_version, "sha256": args.ledger_sha256})
        funding = with_ledger_client(reserve, scope, args.ledger_sha256, args.ledger_version, request)
        tx.checkpoint("funding", funding)
        enrolled["metadata"]["labels"] = {**job["metadata"]["labels"],
                                          "airaware.training/baseline-hold": funding["revision"]}
        tx.checkpoint("enrollment", enrolled)
        tx.checkpoint("enroll-started")
        # On a lost create reply, read back this name and exact attempt label.
        # An absent result is ambiguous. Never issue another create or release.
        try:
            created = json.loads(tx.call(*create, payload=enrolled))
        except (subprocess.SubprocessError, OSError):
            created = tx.live("jobs", name)
            if not created:
                raise
        uid = verified_enrollment(enrolled, created)
        if verified_enrollment(enrolled, tx.live("jobs", name)) != uid:
            raise ValueError("Enrollment readback differs from the issued UID")
        tx.checkpoint("job-uid", uid)
        tx.intent = session_intent(target, identity, args.field_manager, deployment, images, held, guard, enrolled, uid)
        tx.checkpoint("intent", tx.intent)
        try:
            tx.activate(images, cutoff)
            verify_placement(client, request, binding, scope["project_id"])
            if current_release() != (images, deployment):
                raise ValueError("Signed deployment changed at dispatch")
            tx.checkpoint("dispatch-schedulers", check_schedulers(scope, request["scope_sha256"]))
            N["check_active"](tx.intent["active"], guard, tx.policy_list())
            with_ledger_client(check_reserved, scope, funding)
            changes = tx.release_patch()
            tx.checkpoint("release-started", changes)
            try:
                result = tx.patch(changes)
            except (subprocess.SubprocessError, OSError):
                result = tx.live("jobs", name)
                if not result or result["metadata"]["uid"] != uid or result["spec"]["suspend"] is not False:
                    raise
            released = copy.deepcopy(enrolled)
            released["spec"]["suspend"] = False
            if verified_enrollment(released, result) != uid or verified_enrollment(released, tx.live("jobs", name)) != uid:
                raise ValueError("Unconfirmed release of the funded UID")
            tx.checkpoint("submitted", {"namespace": "training", "job": name, "uid": uid, "funding": funding})
        finally:
            tx.close()
        published = with_ledger_client(record_submission, scope, tx.checkpoint_value("submitted"))
        tx.checkpoint("offhost", published)
        return published


def recover_submission(session, scope, scope_hash):
    def checkpoint(name):
        return json.loads(private(session / (name + ".json")))

    funding = checkpoint("funding")
    request = funding["hold"]["request"]
    intent = checkpoint("intent")
    if (not checkpoint("closed-reconciled") or not checkpoint("release-started")
            or scope_hash != request["scope_sha256"] or intent["target"]["cluster_id"] != request["cluster_id"]
            or os.environ["NEBIUS_TRAINING_CLUSTER_ID"] != request["cluster_id"]
            or os.environ["NEBIUS_CA_BUNDLE_SHA256"] != intent["target"]["ca_sha256"]
            or os.environ["NEBIUS_API_ENDPOINT"] != intent["target"]["endpoint"]):
        raise ValueError("Expected the original closed session, dispatch intent and managed target")
    client = kubectl_client_for_identity("training", intent["operator"], direct=True)
    observed = json.loads(output(*client, "--request-timeout=10s", "get", "job", intent["job"], "-o", "json"))
    expected = checkpoint("enrollment")
    expected["spec"]["suspend"] = False
    uid = verified_enrollment(expected, observed)
    if uid != checkpoint("job-uid") or uid != intent["job_uid"]:
        raise ValueError("Observed Job differs from the dispatched UID")
    verify_bucket_projects(scope)
    return with_ledger_client(record_submission, scope, {"namespace": "training", "job": intent["job"],
                                                         "uid": uid, "funding": funding})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    describe = sub.add_parser("describe", help="Emit the exact baseline_approval for the funding owner")
    launch = sub.add_parser("submit", help="Reserve one attempt, enroll it, release it, revoke access")
    for command in (describe, launch):
        for key in ("review", "scope", "binding", "staging"):
            command.add_argument("--" + key, type=Path, required=True)
        command.add_argument("--node-uid", required=True)
        command.add_argument("--expected-deployment", required=True)
    launch.add_argument("--field-manager", required=True)
    launch.add_argument("--session", type=Path, required=True)
    launch.add_argument("--ledger-sha256", required=True)
    launch.add_argument("--ledger-version", required=True)
    recover = sub.add_parser("record", help="Reconcile a dispatched UID and publish its off-host receipt")
    recover.add_argument("--session", type=Path, required=True)
    recover.add_argument("--scope", type=Path, required=True)
    args = parser.parse_args()
    scope_raw = private(args.scope)
    scope = json.loads(scope_raw)
    if args.action == "record":
        print(json.dumps(recover_submission(args.session, scope, sha(scope_raw)), sort_keys=True))
        return
    images, deployment = current_release()
    candidate = json.loads((ROOT / "bootstrap/nebius/releases/flamingo-candidate.json").read_bytes())
    roles = {role: item["repository"] + "@" + item["index"] for role, item in candidate["images"].items()}
    if roles != {role: images["flamingo-" + role] for role in ("trainer", "preflight")}:
        raise ValueError("Signed release differs from the checked-in Flamingo candidate")
    request, job, binding = baseline_request(args, scope, sha(scope_raw), images, deployment)
    result = request if args.action == "describe" else submit(args, scope, request, job, binding, images, deployment)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    os.umask(0o077)
    signal.signal(signal.SIGTERM, N["interrupt"])
    main()
