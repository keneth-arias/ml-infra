"""Exercise managed API admission as the short-lived native training submitter."""

import argparse
import json
import re
from pathlib import Path
import subprocess
import sys

SKY = Path(__file__).resolve().parents[1] / "sky"
sys.path.insert(0, str(SKY))
from production_target import kubectl_client


def render_job(mode, trainer, preflight, *, paired=False):
    return json.loads(subprocess.check_output([
        sys.executable, SKY / "episode-profile.py", mode, "fit", "direct", "access-check",
        "--target", "prod-nebius", "--image", trainer,
        *(["--preflight-image", preflight] if mode == "preflight" else []),
        "--bundle-sha256", "b" * 64, "--schedule", "14084", "--step", "14084",
        *(["--cached-weights-sha256", "c" * 64, "--validation-sha256", "d" * 64,
           "--direct-objective", "physical-mae"] if paired else []),
    ]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trainer-image", help="Verified image from the signed release")
    parser.add_argument("--preflight-image", help="Verified image from the same signed release")
    args = parser.parse_args()
    released = bool(args.trainer_image or args.preflight_image)
    if released and not all(ref and re.fullmatch(r"ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}", ref)
                            for ref in (args.trainer_image, args.preflight_image)):
        parser.error("Supply both signed registry digests to verify an activated release")
    kube = kubectl_client("training", "episode-submit", direct=True)
    for namespace, verb, resource, allowed in (
        ("training", "create", "jobs.batch", released),
        ("training", "create", "pods", False),
        ("training", "get", "secrets", False),
        ("training", "create", "persistentvolumeclaims", False),
        ("episode-scoring", "get", "secrets", False),
        ("episode-scoring", "get", "persistentvolumeclaims", False),
        ("episode-scoring", "get", "pods/log", False),
        ("episode-scoring", "create", "jobs.batch", False),
    ):
        resource, _, subresource = resource.partition("/")
        cmd = [*kube, "auth", "can-i", verb, resource, "--namespace", namespace]
        if subresource:
            cmd.append("--subresource=" + subresource)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if (result.returncode, result.stdout.strip()) != ((0, "yes") if allowed else (1, "no")):
            raise SystemExit(f"Unexpected permission for {verb} {resource}/{subresource} in {namespace}")
        print(namespace, verb, resource, subresource, result.stdout.strip())

    def check(job, label, denial=None):
        result = subprocess.run(
            [*kube, "create", "--dry-run=server", "-f", "-", "-o", "name"],
            input=json.dumps(job), capture_output=True, text=True, timeout=20,
        )
        if denial is None:
            valid = result.returncode == 0 and result.stdout.startswith("job.batch/")
        else:
            valid = result.returncode != 0 and "episode-submit-inputs" in result.stderr and denial in result.stderr
        if not valid:
            raise SystemExit(f"{label}: unexpected admission response: {result.stderr.strip()}")
        print(label, "admitted" if denial is None else "denied")

    invalid = "example.invalid/probe@sha256:" + "a" * 64
    if not released:
        # Authorization is the outer boundary during the CPU pilot.
        result = subprocess.run([*kube, "create", "--dry-run=server", "-f", "-"],
                                input=json.dumps(render_job("job", invalid, None)),
                                capture_output=True, text=True, timeout=20)
        if result.returncode == 0 or "cannot create resource" not in result.stderr or "jobs" not in result.stderr:
            raise SystemExit("Expected the held submitter to be refused Job creation by RBAC")
        print("native training submission held")
        return
    for mode in ("preflight", "job"):
        trainer = invalid if mode == "job" else args.trainer_image
        preflight = invalid if mode == "preflight" else args.preflight_image
        check(render_job(mode, trainer, preflight), "unapproved-" + mode, "approved image")
        check(render_job(mode, args.preflight_image, args.trainer_image), "swapped-" + mode, "approved image")
        check(render_job(mode, args.trainer_image, args.preflight_image), mode)
        check(render_job(mode, args.trainer_image, args.preflight_image, paired=True), "paired-" + mode)
    for pool in (None, "training-gpu"):
        job = render_job("preflight", args.trainer_image, args.preflight_image)
        pod = job["spec"]["template"]["spec"]
        if pool is None:
            del pod["nodeSelector"]
        else:
            pod["nodeSelector"]["airaware.training/pool"] = pool
        check(job, "misplaced-preflight", "matching CPU or GPU training pool")
    job = render_job("preflight", args.trainer_image, args.preflight_image)
    job["spec"]["activeDeadlineSeconds"] = 601
    check(job, "overlong-preflight", "at most 600 seconds")
    for label, field, value, message in (
        ("other-account", "serviceAccountName", "default", "token-free"),
        ("mounted-token", "automountServiceAccountToken", True, "token-free"),
        ("other-claim", "volumes", [{"name": "inputs", "persistentVolumeClaim": {"claimName": "scoring-fixture"}},
                                   {"name": "state", "emptyDir": {}}], "read-only training inputs"),
        ("writable-input", "volumes", [{"name": "inputs", "persistentVolumeClaim": {"claimName": "airaware-checkpoints"}},
                                      {"name": "state", "emptyDir": {}}], "read-only training inputs"),
    ):
        job = render_job("job", args.trainer_image, args.preflight_image)
        job["spec"]["template"]["spec"][field] = value
        check(job, label, message)
    job = render_job("job", args.trainer_image, args.preflight_image)
    job["spec"]["template"]["spec"]["containers"][0]["env"].append({
        "name": "FIXTURE", "valueFrom": {"secretKeyRef": {"name": "scoring-fixture", "key": "marker"}},
    })
    check(job, "unapproved-secret-reference", "training tracking URI")


if __name__ == "__main__":
    main()
