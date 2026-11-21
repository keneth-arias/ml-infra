"""Exercise managed API admission as the short-lived native training submitter."""

import argparse
import json
import re
from pathlib import Path
import subprocess
import sys

SKY = Path(__file__).resolve().parents[1] / "sky"
sys.path.insert(0, str(SKY))
from production_release import release_images, require_image
from production_target import kubectl_client


def render_job(mode, trainer, preflight, *, paired=False, attempt=1, restore=False, phase="fit", head="direct"):
    return json.loads(subprocess.check_output([
        sys.executable, SKY / "episode-profile.py", mode, phase, head, "access-check",
        "--target", "prod-nebius", "--image", trainer,
        *(["--preflight-image", preflight] if mode == "preflight" else []),
        "--bundle-sha256", "b" * 64, "--step", "14084", "--attempt", str(attempt),
        *(["--schedule", "14084"] if phase == "fit" else []),
        *(["--resume", "--resume-checkpoint", "500:" + "a" * 64,
           "--graceful-stop", "--gpu-limit-bytes", "16000000000"] if restore else []),
        *(["--cached-weights-sha256", "c" * 64, "--validation-sha256", "d" * 64,
           "--direct-objective", "physical-mae"] if paired else []),
    ]))


def check_permissions(kube, released):
    for namespace, verb, resource, allowed in (
        ("training", "create", "jobs.batch", released),
        ("training", "create", "pods", False),
        ("training", "create", "pods/exec", False),
        ("training", "get", "pods/exec", False),
        ("training", "get", "secrets", False),
        ("training", "create", "persistentvolumeclaims", False),
        ("episode-scoring", "get", "secrets", False),
        ("episode-scoring", "get", "persistentvolumeclaims", False),
        ("episode-scoring", "get", "pods/log", False),
        ("episode-scoring", "create", "pods/exec", False),
        ("episode-scoring", "get", "pods/exec", False),
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trainer-image", help="Verified image from the signed release")
    parser.add_argument("--preflight-image", help="Verified image from the same signed release")
    args = parser.parse_args()
    released = bool(args.trainer_image or args.preflight_image)
    if released and not all(ref and re.fullmatch(r"ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}", ref)
                            for ref in (args.trainer_image, args.preflight_image)):
        parser.error("Supply both signed registry digests to verify an activated release")
    if released:
        images = release_images()
        require_image(images, "trainer", args.trainer_image)
        require_image(images, "preflight", args.preflight_image)
    kube = kubectl_client("training", "episode-submit", direct=True)
    check_permissions(kube, released)

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
    exercise_release(args.trainer_image, args.preflight_image, check)


def exercise_release(trainer_image, preflight_image, check):
    invalid = "example.invalid/probe@sha256:" + "a" * 64
    for mode in ("preflight", "job"):
        trainer = invalid if mode == "job" else trainer_image
        preflight = invalid if mode == "preflight" else preflight_image
        check(render_job(mode, trainer, preflight), "unapproved-" + mode, "approved image")
        check(render_job(mode, preflight_image, trainer_image), "swapped-" + mode, "approved image")
        check(render_job(mode, trainer_image, preflight_image), mode)
        check(render_job(mode, trainer_image, preflight_image, paired=True), "paired-" + mode)
        check(render_job(mode, trainer_image, preflight_image, paired=True, attempt=2, restore=True), "retry-" + mode)
        check(render_job(mode, trainer_image, preflight_image, paired=True, attempt=2), "fresh-retry-" + mode)
        check(render_job(mode, trainer_image, preflight_image, phase="infer", head="quantile"), "inference-" + mode)

        for field, value in (("parallelism", 2), ("completions", 2), ("backoffLimit", 1), ("suspend", True)):
            job = render_job(mode, trainer_image, preflight_image)
            job["spec"][field] = value
            check(job, f"{field}-{mode}", "single active Job completion")
        for resource, value in (("cpu", "500m"), ("memory", "128Mi"), ("ephemeral-storage", "1Gi")):
            job = render_job(mode, trainer_image, preflight_image)
            requests = job["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]
            # Keep each mutant Kubernetes-valid so the admission policy is the observed denial.
            requests[resource] = value
            check(job, f"{resource}-request-{mode}", "approved CPU, memory and device resources")
        job = render_job(mode, trainer_image, preflight_image, paired=True, attempt=2, restore=True)
        command = job["spec"]["template"]["spec"]["containers"][0]["command"]
        pin = command.index("--resume-checkpoint")
        del command[pin:pin + 2]
        check(job, "missing-restore-pin-" + mode, "episode entrypoint")
        job = render_job(mode, trainer_image, preflight_image, paired=True)
        mount = next(m for m in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if m["mountPath"] == "/bundle")
        mount["subPath"] = "airaware/cache/bundles/" + "a" * 64
        check(job, "wrong-cache-pin-" + mode, "checkpoint mount paths")

        for label, override in (
            ("args-override", {"args": ["--attempt", "2"]}),
            ("shell", {"command": ["/bin/sh", "-c", "true"]}),
            ("runtime-env", {"env": [{"name": "PYTHONPATH", "value": "/inputs"}]}),
            ("lifecycle", {"lifecycle": {"postStart": {"exec": {"command": ["/bin/true"]}}}}),
        ):
            job = render_job(mode, trainer_image, preflight_image)
            container = job["spec"]["template"]["spec"]["containers"][0]
            if label == "runtime-env":
                container["env"].extend(override["env"])
            else:
                container.update(override)
            check(job, label + "-" + mode, "approved environment" if label == "runtime-env" else "episode entrypoint")

        job = render_job(mode, trainer_image, preflight_image)
        job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] = "/opt/ml-infra"
        check(job, "program-mount-" + mode, "checkpoint mount paths")
        job = render_job(mode, trainer_image, preflight_image)
        job["spec"]["template"]["spec"]["containers"][0]["command"][1] = "/opt/ml-infra/stage_episode_inputs.py"
        check(job, "staging-entrypoint-" + mode, "episode entrypoint")

    for deadline in (None, 356401):
        job = render_job("job", trainer_image, preflight_image)
        if deadline is None:
            del job["spec"]["activeDeadlineSeconds"]
        else:
            job["spec"]["activeDeadlineSeconds"] = deadline
        check(job, f"gpu-deadline-{deadline}", "positive Job deadline")
    job = render_job("preflight", trainer_image, preflight_image)
    job["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "1Gi"
    check(job, "preflight-memory-limit", "approved CPU, memory and device resources")
    job = render_job("preflight", trainer_image, preflight_image)
    pod = job["spec"]["template"]["spec"]
    pod["containers"][0]["command"].pop()
    check(job, "missing-preflight", "episode entrypoint")
    job = render_job("preflight", trainer_image, preflight_image)
    job["spec"]["template"]["spec"]["containers"][0]["env"].append({
        "name": "MLFLOW_TRACKING_TOKEN", "valueFrom": {"secretKeyRef": {"name": "episode-tracking", "key": "token"}}
    })
    check(job, "preflight-tracking", "approved environment")
    for pool in (None, "training-gpu"):
        job = render_job("preflight", trainer_image, preflight_image)
        pod = job["spec"]["template"]["spec"]
        if pool is None:
            del pod["nodeSelector"]
        else:
            pod["nodeSelector"]["airaware.training/pool"] = pool
        check(job, "misplaced-preflight-" + str(pool), "matching CPU or GPU training pool")
    job = render_job("preflight", trainer_image, preflight_image)
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
        job = render_job("job", trainer_image, preflight_image)
        job["spec"]["template"]["spec"][field] = value
        check(job, label, message)
    job = render_job("job", trainer_image, preflight_image)
    job["spec"]["template"]["spec"]["containers"][0]["env"].append({
        "name": "FIXTURE", "valueFrom": {"secretKeyRef": {"name": "scoring-fixture", "key": "marker"}},
    })
    check(job, "unapproved-secret-reference", "training tracking URI")


if __name__ == "__main__":
    main()
