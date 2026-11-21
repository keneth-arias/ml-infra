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
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

from render import NS, SA

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from worker_cleanup import cleanup_digest, plan_release, scope_digest, verify_inventory

ENDPOINT = "https://storage.eu-south1.nebius.cloud"
STATE_KEY = "prod/mk8s/terraform.tfstate"


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
            or scheduled != cron["status"]["lastScheduleTime"]
            or any(obj["metadata"].get("deletionTimestamp") for obj in (pod, job, cron))
            or spec["suspend"] or spec["schedule"] != "* * * * *" or spec["concurrencyPolicy"] != "Forbid"
            or spec["timeZone"] != "Etc/UTC" or spec["startingDeadlineSeconds"] != 45):
        raise ValueError("Unexpected, suspended or stale cleanup scheduler")
    templates = [spec["jobTemplate"]["spec"]["template"]["spec"], job["spec"]["template"]["spec"], pod["spec"]]
    image = templates[0]["containers"][0]["image"]
    for template in templates:
        if (template["serviceAccountName"] != SA or template["automountServiceAccountToken"] is not False
                or len(template["containers"]) != 1 or template["containers"][0]["image"] != image):
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


def run_tick(run, stage, rehearsal):
    if rehearsal:
        evidence = scheduler(run)
        check_tick(evidence, evidence, utc())
        for host in ("api.nebius.cloud", "storage.eu-south1.nebius.cloud"):
            with socket.create_connection((host, 443), timeout=5) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                    pass
        print(json.dumps({"rehearsal": True, "scheduler": evidence}, sort_keys=True))
        return
    scope = json.loads(Path("/scope/scope.json").read_bytes())
    raw = Path("/scope/inventory.json").read_bytes()
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
                            "--inventory", "/scope/inventory.json"], check=True)
            return
        verify_inventory("ml-prod", scope["project_id"], scope["cluster_id"], baseline, before_stage=scope)
        groups, _ = plan_release(Path(scratch) / "rehearsal.tfplan", profile="ml-prod", cluster_id=scope["cluster_id"],
                                 run_id=run, cutoff_label=cutoff.strftime("%Y%m%d%H%M%S"))
        if groups != ({scope["support_group_id"]} if stage == "gpu" else set()):
            raise ValueError("Unexpected groups in locked fw13 release rehearsal")
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
        import boto3
        store = boto3.client("s3", region_name="eu-south1", endpoint_url=ENDPOINT)
        response = store.put_object(Bucket=scope["state_bucket"],
                                    Key=f"prod/mk8s/release-control/timers/{run}/{stage}/fw13.json",
                                    Body=json.dumps(proof).encode(), ContentType="application/json")
        if response.get("VersionId") in (None, "", "null"):
            raise ValueError("Missing storage version for fw13 proof")
        print("Published fw13 CronJob proof", response["VersionId"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run")
    parser.add_argument("stage", choices=("pilot", "support", "gpu"))
    parser.add_argument("--rehearsal", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    # Leave time inside the Job deadline to report a hung Terraform/provider operation.
    # The subprocess group also covers the provider subprocesses Terraform starts.
    if os.environ.get("CLEANUP_SUPERVISED") == "1":
        try:
            run_tick(args.run, args.stage, args.rehearsal)
        except Exception as exc:
            print("Cleanup check failed:", type(exc).__name__, file=sys.stderr, flush=True)
            sys.exit(1)
        sys.exit(0)
    try:
        child = subprocess.Popen([sys.executable, __file__, *sys.argv[1:]], start_new_session=True,
                                 env={**os.environ, "CLEANUP_SUPERVISED": "1"})
        try:
            code = child.wait(timeout=640)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
        if code:
            raise RuntimeError("Backup process failed")

    except Exception as exc:
        print("fw13 cleanup failed:", type(exc).__name__, file=sys.stderr, flush=True)
        if not args.rehearsal:
            notify = runpy.run_path(str(ROOT / "management/notify-cleanup.py"))
            os.environ.update(MONITOR_INVOCATION_ID=os.environ["POD_UID"], CREDENTIALS_DIRECTORY="/identity",
                              CLEANUP_OTLP_LOGS_URL="https://fw13pro.degu-fir.ts.net:8443/insert/opentelemetry/v1/logs")
            if Path("/identity/otlp-ca.pem").exists():
                os.environ["SSL_CERT_FILE"] = "/identity/otlp-ca.pem"
            notify["main"](args.run, host="fw13")
        sys.exit(1)
