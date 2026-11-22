"""Optional storage-owner operation inside two newly bound, empty subPath mounts."""

import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path
import runpy
import stat
from contextlib import ExitStack


def prepare(directories):
    with ExitStack() as opened:
        leaves = []
        for path in directories:
            fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
            opened.callback(os.close, fd)
            info = os.fstat(fd)
            if any((info.st_dev, info.st_ino) == (prior.st_dev, prior.st_ino) for _, prior in leaves):
                raise ValueError("Expected two distinct leaves")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            if (info.st_uid not in (0, 1000) or os.listdir(fd) or info.st_mode & 0o700 != 0o700
                    or info.st_mode & (stat.S_ISUID | stat.S_ISVTX)):
                raise ValueError("Expected fresh empty leaf directories with owner rwx")
            leaves.append((fd, info))
        for fd, info in leaves:
            if info.st_uid == 0:
                os.fchown(fd, 1000, -1)
            if os.fstat(fd).st_uid != 1000 or os.listdir(fd):
                raise ValueError("Owner operation requires attended reconciliation")
            os.fsync(fd)


def owner_job(stage, source):
    pod = stage["spec"]["template"]["spec"]
    container = pod["containers"][0]
    owner = {
        "name": "prepare-leaves", "image": container["image"], "imagePullPolicy": "IfNotPresent",
        "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File",
        "command": ["/usr/local/bin/python", "-I", "-B", "-c", source, "--prepare-two-leaves"],
        "resources": {"requests": {"cpu": "100m", "memory": "32Mi"}, "limits": {"cpu": "1", "memory": "64Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]}},
        "volumeMounts": [mount for mount in container["volumeMounts"] if mount["name"] == "claim"],
    }
    pod = {key: pod[key] for key in ("restartPolicy", "automountServiceAccountToken", "enableServiceLinks",
                                     "nodeSelector", "affinity", "imagePullSecrets", "priority",
                                     "preemptionPolicy", "tolerations")}
    pod.update(serviceAccountName="default", containers=[owner], dnsPolicy="ClusterFirst",
               schedulerName="default-scheduler", terminationGracePeriodSeconds=30,
               securityContext={"runAsUser": 0, "runAsGroup": 0, "seccompProfile": {"type": "RuntimeDefault"}},
               volumes=[{"name": "claim", "persistentVolumeClaim": {"claimName": "airaware-checkpoints"}}])
    labels = {"airaware.training/phase": "public-flamingo-owner", "airaware.training/run": stage["metadata"]["labels"]["airaware.training/run"]}
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": stage["metadata"]["name"] + "-owner", "namespace": "training", "labels": labels},
            "spec": {"suspend": True, "parallelism": 1, "completions": 1, "backoffLimit": 0, "podReplacementPolicy": "Failed",
                     "activeDeadlineSeconds": 60, "template": {"metadata": {"labels": labels}, "spec": pod}}}


def mounted_worker():
    paths = [Path("/base"), Path("/output")]
    mounts = {line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    if os.geteuid() != 0 or not {str(path) for path in paths} <= mounts:
        raise SystemExit("Expected the approved owner process and two exact bind mounts")
    prepare(paths)


def worker_source():
    # Keep the management entry points out of both the root container and the CEL
    # comparison of its command. Only the two-leaf filesystem operation is reviewable here.
    from inspect import getsource
    imports = "import fcntl, os, stat\nfrom contextlib import ExitStack\nfrom pathlib import Path\n"
    return "\n".join((imports, getsource(prepare), getsource(mounted_worker), "mounted_worker()"))


def owner_transition(enrolled, active):
    same = ("purpose", "run", "deployment_sha256", "packet_file_sha256", "owner_job_file_sha256",
            "leaf_operator", "storage_operator", "staging_operator", "exclusive_storage_handoff",
            "fresh_empty_leaves", "cutoff")
    if (enrolled["action"] != "enroll" or active["action"] != "activate"
            or any(active[k] != enrolled[k] for k in same)
            or active["expires_at"] != enrolled["expires_at"]):
        raise ValueError("Leaf activation differs from its approved suspended enrollment")
    closed, opened = enrolled["closed_guard"], active["owner_guard"]
    if (any(closed[k] != opened[k] for k in ("policy_uid", "binding_uid", "binding_generation"))
            or opened["policy_generation"] != closed["policy_generation"] + 1):
        raise ValueError("Owner guard must directly succeed the enrolled closed guard")


def verify_owner_job(closeout, reviewed, live, job_uid, *, suspended, expected_rv=None):
    name = reviewed["metadata"]["name"]
    meta = closeout["checked_meta"](live, name, job_uid)
    if (live.get("apiVersion") != "batch/v1" or live.get("kind") != "Job" or meta.get("ownerReferences")
            or not meta.get("resourceVersion") or (expected_rv is not None and meta["resourceVersion"] != expected_rv)):
        raise ValueError("Owner Job identity or resource version differs from approval")
    if suspended:
        status = live.get("status", {})
        if (meta.get("generation") != 1 or any(status.get(key) for key in
                ("active", "succeeded", "failed", "startTime", "completionTime", "terminating"))
                or any(c.get("type") != "Suspended" for c in status.get("conditions", []))):
            raise ValueError("Owner operation requires a fresh suspended Job")
    if reviewed["spec"]["suspend"] is not True:
        raise ValueError("Only the suspended owner manifest may be enrolled")
    # Reuse the verified stage Job comparator, which expects a running review
    # template and handles Kubernetes' generated labels/selector and defaults.
    expected = {**reviewed, "spec": {k: v for k, v in reviewed["spec"].items() if k != "suspend"}}
    closeout["verify_job"]({"job": expected}, live, suspended=suspended)
    return meta


def owner_patch(job_uid, resource_version):
    return [{"op": "test", "path": "/metadata/uid", "value": job_uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": resource_version},
            {"op": "test", "path": "/spec/suspend", "value": True},
            {"op": "replace", "path": "/spec/suspend", "value": False}]


def operate(argv):
    import argparse
    from datetime import datetime, timezone
    from private_api import pinned_kubeconfig_ca
    from production_target import kubectl_client_for_identity
    from public_flamingo_stage_guard import verify_guard

    parser = argparse.ArgumentParser(description="Enroll or activate the approved empty-leaf ownership Job")
    parser.add_argument("action", choices=("enroll", "activate"))
    for name in ("approval", "promotion", "binding", "scope", "owner-job", "packet"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--approved-sha256", required=True)
    parser.add_argument("--enrollment-approval", type=Path)
    a = parser.parse_args(argv)
    here = Path(__file__).resolve().parent
    submit = runpy.run_path(str(here / "submit-public-flamingo-stage.py"))
    closeout = runpy.run_path(str(here / "public-flamingo-stage-closeout.py"))
    render = runpy.run_path(str(here / "render-public-flamingo-stage.py"))
    native = runpy.run_path(str(here.parents[1] / "bootstrap/nebius/management/native-activation.py"))
    approval = submit["read_approval"](a.approval, a.approved_sha256)
    if (approval["action"] != a.action or approval["purpose"] != "public-flamingo-prepare-leaves"
            or approval["exclusive_storage_handoff"] is not True or approval["fresh_empty_leaves"] is not True
            or bool(a.enrollment_approval) != (a.action == "activate")):
        raise ValueError("Expected separate owner approval for fresh empty leaves on this action")
    if a.action == "activate":
        enrolled = submit["read_approval"](a.enrollment_approval, approval["enrollment_approval_sha256"])
        owner_transition(enrolled, approval)
    scope = closeout["pinned_file"](a.scope, submit["SCOPE_SHA256"])
    run = approval["run"]

    def reproduce():
        packet = render["build_packet"](a.promotion, a.binding, approval["deployment_sha256"], run, "stage-" + run + "-inventory")
        submit["approved_packet"](a.packet, approval["packet_file_sha256"], packet)
        job = owner_job(packet["job"], worker_source())
        submit["approved_packet"](a.owner_job, approval["owner_job_file_sha256"], job)
        return packet, job

    packet, job = reproduce()
    identities = (approval["leaf_operator"], approval["storage_operator"], approval["staging_operator"])
    if (len(set(identities)) != 3 or any(not ident or ident == "system:anonymous" or
            ident in {"system:serviceaccount:training:" + role for role in
                      ("default", "episode-worker", "episode-input-stager", "episode-submit")} for ident in identities)
            or approval["storage_operator"] != os.environ["NEBIUS_STORAGE_OPERATOR_IDENTITY"]
            or os.environ["NEBIUS_TRAINING_CLUSTER_ID"] != packet["cluster_id"]
            or scope["cluster_id"] != packet["cluster_id"] or scope["project_id"] != packet["project_id"]
            or approval["cutoff"] != scope["cutoff"]):
        raise ValueError("Leaf owner, managed target or deadline differs from the original handoff")
    kube = kubectl_client_for_identity("training", approval["leaf_operator"], direct=True)
    storage_profile = Path(os.environ["NEBIUS_STORAGE_KUBECONFIG"])
    if storage_profile.stat().st_mode & 0o077:
        raise ValueError("Expected the private storage observer context")
    observer = ["kubectl", "--kubeconfig", str(storage_profile), "--context", "prod-nebius"]
    pinned_kubeconfig_ca(observer, packet["cluster_id"], os.environ["NEBIUS_CA_BUNDLE_SHA256"],
                         endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"), direct=True)
    def read(*args):
        return subprocess.check_output([*observer, "--request-timeout=10s", *args], timeout=15)
    if read("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}").decode() != approval["storage_operator"]:
        raise ValueError("Unexpected storage observer")
    def get(*args):
        return json.loads(read("get", *args, "-o", "json"))

    phase = "closed" if a.action == "enroll" else "owner"
    pins = approval[phase + "_guard"]
    job_uid = closeout["uid"](pins["job_uid"]) if phase == "owner" else None
    cutoff = min(closeout["timestamp"](scope["cutoff"]), closeout["timestamp"](approval["expires_at"]))
    def guard():
        return verify_guard(get, run, phase, pins, job=job if phase == "owner" else None)
    def time_check():
        submit["check_time"](cutoff, job["spec"]["activeDeadlineSeconds"], datetime.now(timezone.utc))
    name = job["metadata"]["name"]
    time_check()
    guard()
    native["check_active"](*native["held_policy_and_binding"](), {"items": [get(native["POLICY"]), get(native["GUARD"])]})
    closeout["exclude_other_writers"](get, job_uid)
    submit["check_placement"](packet["placement"], get("node", packet["placement"]["name"]))
    env = dict(os.environ, NEBIUS_STORAGE_BINDING_RECEIPT=str(a.binding), NEBIUS_STORAGE_BINDING_SHA256=packet["binding_sha256"])
    subprocess.run([sys.executable, str(here / "training-storage-handoff.py")], env=env, check=True, timeout=140)
    reproduce()
    guard()
    time_check()
    if a.action == "enroll":
        job_uid = submit["create"](kube, job)
        print(f"Enrolled suspended {name} UID {job_uid}", file=sys.stderr, flush=True)
        guard()
        meta = verify_owner_job(closeout, job, get("job", name, "-n", "training"), job_uid, suspended=True)
    else:
        meta = verify_owner_job(closeout, job, get("job", name, "-n", "training"), job_uid, suspended=True,
                                expected_rv=approval["job_resource_version"])
        if not isinstance(approval["job_resource_version"], str) or not approval["job_resource_version"]:
            raise ValueError("Expected independently approved owner Job resourceVersion")
        patch = owner_patch(job_uid, meta["resourceVersion"])
        result = subprocess.run([*kube, "--request-timeout=30s", "patch", "job", name, "--type=json",
                                 "--patch", json.dumps(patch), "-o", "json"], capture_output=True, timeout=35)
        if result.returncode:
            raise ValueError(f"Owner activation unconfirmed; inspect {name} UID {job_uid} before any retry")
        meta = verify_owner_job(closeout, job, json.loads(result.stdout), job_uid, suspended=False)
    print(json.dumps({"action": a.action, "job": name, "job_uid": job_uid, "job_resource_version": meta["resourceVersion"],
                      "approval_sha256": a.approved_sha256, "owner_job_file_sha256": approval["owner_job_file_sha256"]},
                     sort_keys=True))


def main():
    if sys.argv[1:2] == ["render"]:
        script = Path(__file__).resolve()
        reviewed = subprocess.check_output([sys.executable, str(script.with_name("render-public-flamingo-stage.py")),
                                            *sys.argv[2:]])
        print(json.dumps(owner_job(json.loads(reviewed)["job"], worker_source()), indent=2, sort_keys=True))
        return
    operate(sys.argv[1:])


if __name__ == "__main__":
    main()
