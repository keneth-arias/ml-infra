"""Run the fw13 cutoff worker, or attest a controller-scheduled tick before allocation."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from render import NS, SA

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from control_storage import control_bucket, publish_timer_proof
from worker_cleanup import cleanup_digest, cloud_items, plan_release, scope_digest, verify_inventory

ENDPOINT = "https://storage.eu-south1.nebius.cloud"
STATE_KEY = "prod/mk8s/terraform.tfstate"
ALERT_CA = "/identity/otlp-ca.pem"
ALERT_URL = "https://192.168.50.155:8443/insert/opentelemetry/v1/logs"


def utc():
    return datetime.now(timezone.utc)


def stamp(t):
    return t.isoformat().replace("+00:00", "Z")


def get_object(group, resource, name):
    prefix = "/apis/batch/v1" if group == "batch" else "/api/v1"
    token = Path("/observer/token").read_text().strip()
    url = f"https://kubernetes.default.svc{prefix}/namespaces/{NS}/{resource}/{name}"
    request = Request(url, headers={"Authorization": "Bearer " + token})
    with urlopen(request, context=ssl.create_default_context(cafile="/observer/ca.crt"), timeout=5) as response:
        return json.load(response)


def owner(obj, kind):
    refs = [r for r in obj["metadata"]["ownerReferences"] if r.get("controller") is True]
    if len(refs) != 1 or refs[0]["kind"] != kind:
        raise ValueError("Tick lacks its expected controller")
    return refs[0]


def scheduler(run, *, get=get_object):
    pod = get("", "pods", os.environ["POD_NAME"])
    jobref = owner(pod, "Job")
    job = get("batch", "jobs", jobref["name"])
    cronref = owner(job, "CronJob")
    cron = get("batch", "cronjobs", "expiry-" + run)
    scheduled = job["metadata"]["annotations"]["batch.kubernetes.io/cronjob-scheduled-timestamp"]
    spec = cron["spec"]
    if (pod["metadata"]["uid"] != os.environ["POD_UID"] or jobref["uid"] != job["metadata"]["uid"]
            or cronref["uid"] != cron["metadata"]["uid"] or cronref["name"] != cron["metadata"]["name"]
            or datetime.fromisoformat(scheduled) != datetime.fromisoformat(cron["status"]["lastScheduleTime"])
            or any(obj["metadata"].get("deletionTimestamp") for obj in (pod, job, cron))
            or spec["suspend"] or spec["schedule"] != "* * * * *" or spec["concurrencyPolicy"] != "Forbid"
            or spec["timeZone"] != "Etc/UTC" or spec["startingDeadlineSeconds"] != 45):
        raise ValueError("Unexpected, suspended or stale cleanup scheduler")
    templates = [spec["jobTemplate"]["spec"]["template"]["spec"], job["spec"]["template"]["spec"], pod["spec"]]
    image = templates[0]["containers"][0]["image"]
    desired = templates[0]["containers"][0]
    for template in templates:
        if (template["serviceAccountName"] != SA or template["automountServiceAccountToken"] is not False
                or len(template["containers"]) != 1
                or any(template["containers"][0].get(k) != desired.get(k)
                       for k in ("image", "command", "args", "volumeMounts"))
                or {v["name"]: v for v in template["volumes"]} != {v["name"]: v for v in templates[0]["volumes"]}):
            raise ValueError("Unexpected cleanup Pod identity or image")
    return {"uid": cronref["uid"], "spec_sha256": scope_digest(spec), "image": image,
            "job_uid": jobref["uid"], "pod_uid": pod["metadata"]["uid"], "scheduled_at": scheduled}


def check_tick(proof, expected, now):
    if any(proof[k] != expected[k] for k in ("uid", "spec_sha256", "image")):
        raise ValueError("Scheduler differs from approved fw13 CronJob")
    tick = datetime.fromisoformat(proof["scheduled_at"])
    if not now - timedelta(seconds=60) < tick <= now:
        raise ValueError("No fresh controller-scheduled fw13 tick")
    return tick + timedelta(minutes=1)


def command(*args):
    return subprocess.check_output(args, text=True, timeout=45).strip()


def initialize(scope, key, directory):
    """Fresh disposable CLI config and locked, offline-provider Terraform initialization."""
    account = scope["cleanup_accounts"]["fw13"]
    pem = "/identity/auth.pem"
    os.environ.update(HOME=str(directory), NEBIUS_SA_ID=account, NEBIUS_AUTHORIZED_KEY_ID=key,
                      NEBIUS_PRIVATE_KEY_FILE=pem, AWS_SHARED_CREDENTIALS_FILE="/identity/aws-credentials",
                      AWS_EC2_METADATA_DISABLED="true", TF_WORKSPACE="default")
    profile = {"endpoint": "api.nebius.cloud", "auth-type": "service account", "service-account-id": account,
               "public-key-id": key, "private-key-file-path": pem, "parent-id": scope["project_id"]}
    cfg = directory / ".nebius"
    cfg.mkdir(mode=0o700)
    (cfg / "config.yaml").write_text(json.dumps({"default": "ml-prod", "profiles": {"ml-prod": profile}}))
    who = json.loads(command("nebius", "-p", "ml-prod", "iam", "whoami", "--format", "json"))
    if who["service_account_profile"]["info"]["metadata"]["id"] != account:
        raise ValueError("Wrong cleanup cloud account")
    for name in ("main.tf", "versions.tf", ".terraform.lock.hcl"):
        shutil.copyfile(ROOT / name, directory / name)
    tfvars = Path("/scope/terraform.tfvars").read_bytes()
    if hashlib.sha256(tfvars).hexdigest() != scope["fw13_tfvars_sha256"]:
        raise ValueError("Cleanup inputs differ from approved Terraform variables")
    (directory / "terraform.tfvars").write_bytes(tfvars)
    os.chdir(directory)
    command("terraform", "init", "-input=false", "-lockfile=readonly",
            "-backend-config=bucket=" + scope["state_bucket"], "-backend-config=region=eu-south1",
            '-backend-config=endpoints={s3="' + ENDPOINT + '"}')
    config = json.loads(Path(".terraform/terraform.tfstate").read_bytes())["backend"]
    if (config["type"] != "s3" or config["config"]["key"] != STATE_KEY
            or config["config"]["bucket"] != scope["state_bucket"] or config["config"]["region"] != "eu-south1"
            or config["config"]["endpoints"]["s3"] != ENDPOINT or config["config"]["use_lockfile"] is not True):
        raise ValueError("Unexpected initialized cleanup backend")
    state = json.loads(command("terraform", "state", "pull"))
    if state["lineage"] != scope["state_lineage"]:
        raise ValueError("Unexpected cleanup state lineage")
    return state["serial"]


def verify_allocated_inventory(profile, scope, baseline, groups):
    """Verify a completed allocation against the independently checked release targets."""
    cluster, project = scope["cluster_id"], scope["project_id"]
    stage = scope["stage"]
    support = {scope["support_group_id"]} if stage == "gpu" else set()
    if not support < groups or len(groups) != len(support) + 1:
        raise ValueError("Unexpected run groups after allocation")
    new_group, = groups - support
    cutoff_label = datetime.fromisoformat(scope["cutoff"]).strftime("%Y%m%d%H%M%S")

    def inventory(key, service, resource, parent, support_key):
        rows = cloud_items(profile, service, resource, parent)
        approved = baseline[key]
        if not isinstance(approved, list) or any(not isinstance(row, str) or not row for row in approved):
            raise ValueError("Invalid approved baseline for " + resource)
        original = set(approved)
        if stage == "gpu":
            if scope[support_key] in original:
                raise ValueError("Support resource overlaps the approved baseline")
            original.add(scope[support_key])
        if not original <= rows.keys() or len(rows) != len(original) + 1:
            raise ValueError("Inventory differs from approved baseline plus one " + resource)
        return rows, set(rows) - original

    group_rows, new_groups = inventory(
        "group_ids", "mk8s", "node-group", cluster, "support_group_id"
    )
    vm_rows, new_vms = inventory(
        "instance_ids", "compute", "instance", project, "support_instance_id"
    )
    disk_rows, new_disks = inventory(
        "disk_ids", "compute", "disk", project, "support_boot_disk_id"
    )
    if new_groups != {new_group}:
        raise ValueError("Live group differs from the locked release plan")

    worker_ids = [(new_group, *new_vms, *new_disks)]
    if stage == "gpu":
        worker_ids.append((scope["support_group_id"], scope["support_instance_id"],
                           scope["support_boot_disk_id"]))
    for group_id, vm_id, disk_id in worker_ids:
        group, vm, disk = group_rows[group_id], vm_rows[vm_id], disk_rows[disk_id]
        status = group["status"]
        metadata = group["metadata"]
        group_labels = metadata.get("labels", {})
        if (
            metadata["parent_id"] != cluster
            or group_labels.get("airaware-run") != scope["run_id"]
            or group_labels.get("airaware-cutoff") != cutoff_label
            or status["state"] != "RUNNING"
            or status.get("reconciling")
            or status.get("outdated_node_count") not in (None, 0, "0")
            or vm["status"]["state"] != "RUNNING"
            or any(str(status.get(k)) != "1" for k in
                   ("node_count", "target_node_count", "ready_node_count"))
        ):
            raise ValueError("Allocated group or VM is not owned and healthy")
        resources = vm["spec"]["resources"]
        expected = (("gpu-rtx6000-a", "1gpu-24vcpu-218gb") if stage == "gpu" and group_id == new_group
                    else ("cpu-d3", "4vcpu-16gb"))
        boot = vm["spec"]["boot_disk"]["managed_disk"]
        labels = {"mk8s-cluster-id": cluster, "mk8s-node-group-id": group_id}
        if (
            (resources["platform"], resources["preset"]) != expected
            or boot["name"] != disk["metadata"]["name"]
            or any(row.get("labels", {}).get(k) != v for row in
                   (vm["metadata"], boot, disk["metadata"]) for k, v in labels.items())
        ):
            raise ValueError("VM or boot disk differs from the allocated group")


def run_tick(run, stage, rehearsal, *, scope_dir=Path("/scope")):
    if rehearsal:
        evidence = scheduler(run)
        check_tick(evidence, evidence, utc())
        for host in ("api.nebius.cloud", "storage.eu-south1.nebius.cloud"):
            with socket.create_connection((host, 443), timeout=5) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                    pass
        print(json.dumps({"rehearsal": True, "scheduler": evidence}, sort_keys=True))
        return
    scope = json.loads((scope_dir / "scope.json").read_bytes())
    raw = (scope_dir / "inventory.json").read_bytes()
    baseline = json.loads(raw)
    if ((scope["run_id"], scope["stage"]) != (run, stage)
            or (baseline["project_id"], baseline["cluster_id"]) != (scope["project_id"], scope["cluster_id"])
            or hashlib.sha256(raw).hexdigest() != scope["baseline_sha256"]):
        raise ValueError("CronJob scope or inventory mismatch")
    cutoff = datetime.fromisoformat(scope["cutoff"])
    now = utc()
    if now < cutoff and now > datetime.fromisoformat(scope["fw13_attest_until"]):
        return
    key = scope["fw13_public_key_id"]
    with TemporaryDirectory() as scratch:
        serial = initialize(scope, key, Path(scratch))
        if utc() >= cutoff:
            subprocess.run([str(ROOT / "expire-workers"), "--apply", "--run-id", run, "--cutoff", scope["cutoff"],
                            "--project-id", scope["project_id"], "--cluster-id", scope["cluster_id"],
                            "--inventory", str(scope_dir / "inventory.json")], check=True)
            return
        control_bucket(scope)
        # Missing alert credentials block a new allocation but never prevent cutoff cleanup.
        bearer = Path("/identity/otlp-bearer").read_text().strip()
        if not bearer or any(c.isspace() for c in bearer):
            raise ValueError("Missing or invalid fw13 alert credential")
        # An empty protobuf ExportLogsServiceRequest verifies ingestion and emits no log.
        notifier = runpy.run_path(str(ROOT / "management/notify-cleanup.py"))
        notifier["send"](ALERT_URL, bearer, b"", cafile=ALERT_CA)
        groups, _ = plan_release(Path(scratch) / "rehearsal.tfplan", profile="ml-prod", cluster_id=scope["cluster_id"],
                                 run_id=run, cutoff_label=cutoff.strftime("%Y%m%d%H%M%S"))
        expected = {scope["support_group_id"]} if stage == "gpu" else set()
        if groups != expected:
            verify_allocated_inventory("ml-prod", scope, baseline, groups)
            print("Verified allocated worker inventory without publishing a funding proof")
            return
        verify_inventory("ml-prod", scope["project_id"], scope["cluster_id"], baseline, before_stage=scope)
        observed = scheduler(run)
        now = utc()
        next_tick = check_tick(observed, scope["fw13_cronjob"], now)
        if now >= cutoff or now > datetime.fromisoformat(scope["fw13_attest_until"]):
            raise ValueError("Preallocation proof window ended")
        units = {name: ROOT / "management" / name for name in (
            "worker-expiry@.timer", "worker-expiry@.service", "worker-attest@.service")}
        proof = {k: scope[k] for k in ("stage", "run_id", "cutoff", "project_id", "cluster_id",
                                        "state_lineage", "baseline_sha256")}
        proof.update(host="fw13", timer="cronjob/expiry-" + run, kind_cronjob=observed, enabled=True, active=True,
                     account_id=scope["cleanup_accounts"]["fw13"], provider_key_id=key, state_serial=serial,
                     observed_at=stamp(now), next_tick=stamp(next_tick), scope_sha256=scope_digest(scope),
                     cleanup_source_sha256=cleanup_digest(ROOT, units), cloud_state_access=True,
                     inventory_reconciled=True, locked_release_plan_checked=True)
        if (Path(os.environ["CLEANUP_CONTROL"]) / "stopping").exists():
            raise ValueError("Backup stopping before attestation")
        import boto3
        store = boto3.client("s3", region_name="eu-south1", endpoint_url=ENDPOINT)
        version = publish_timer_proof(store, scope, "fw13", proof)
        print("Published fw13 CronJob proof", version)


def stop_worker(child, control, *, grace=75, terraform_binary="/usr/local/bin/terraform"):
    """Fence new Terraform commands and interrupt the active CLI once so it can unlock."""
    (control / "stopping").touch(mode=0o600)
    until = time.monotonic() + grace
    signaled = False
    while child.poll() is None and time.monotonic() < until:
        if not signaled:
            try:
                pid = int((control / "terraform.pid").read_text())
                fd = os.pidfd_open(pid)
                try:
                    if os.getpgid(pid) == child.pid and os.path.samefile(f"/proc/{pid}/exe", terraform_binary):
                        signal.pidfd_send_signal(fd, signal.SIGINT)
                        signaled = True
                finally:
                    os.close(fd)
            except (FileNotFoundError, ProcessLookupError):
                pass
        time.sleep(0.1)
    if child.poll() is None:
        # A stuck provider may still hold the lock. Do not try to unlock it here.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def supervise(argv, *, timeout=600):
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.signal(sig, request_stop)
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with TemporaryDirectory() as folder:
            control = Path(folder)
            child = subprocess.Popen(argv, start_new_session=True, env={
                **os.environ, "CLEANUP_SUPERVISED": "1", "CLEANUP_CONTROL": str(control)})
            until = time.monotonic() + timeout
            while child.poll() is None:
                remaining = until - time.monotonic()
                if remaining <= 0 or stopping:
                    stop_worker(child, control)
                    raise RuntimeError("Backup interrupted; check state lock and inventory")
                time.sleep(min(0.25, remaining))
            if child.returncode:
                raise RuntimeError("Backup process failed")
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run")
    parser.add_argument("stage", choices=("pilot", "support", "gpu"))
    parser.add_argument("--rehearsal", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    # Leave time inside the Job deadline to report a hung Terraform/provider operation.
    # Kubernetes terminates PID 1, which forwards a bounded graceful stop to the active CLI.
    if os.environ.get("CLEANUP_SUPERVISED") == "1":
        try:
            run_tick(args.run, args.stage, args.rehearsal)
        except Exception as exc:
            print("Cleanup check failed:", type(exc).__name__, file=sys.stderr, flush=True)
            sys.exit(1)
        sys.exit(0)
    try:
        supervise([sys.executable, __file__, *sys.argv[1:]])
    except Exception as exc:
        print("fw13 cleanup failed:", type(exc).__name__, file=sys.stderr, flush=True)
        if not args.rehearsal:
            notify = runpy.run_path(str(ROOT / "management/notify-cleanup.py"))
            os.environ.update(MONITOR_INVOCATION_ID=os.environ["POD_UID"], CREDENTIALS_DIRECTORY="/identity",
                              CLEANUP_OTLP_LOGS_URL=ALERT_URL)
            notify["main"](args.run, host="fw13", cafile=ALERT_CA)
        sys.exit(1)
