"""Exercise managed API admission as the short-lived native training submitter."""

import argparse
from copy import deepcopy
import json
import re
from pathlib import Path
import subprocess
import sys

SKY = Path(__file__).resolve().parents[1] / "sky"
sys.path.insert(0, str(SKY))
from production_release import QWEN_ONLY, release_images, require_image
from production_target import kubectl_client


def render_job(mode, trainer, preflight, *, paired=False, flamingo=False, attempt=1, restore=False,
               phase="fit", head="direct"):
    return json.loads(subprocess.check_output([
        sys.executable, SKY / "episode-profile.py", mode, phase, head, "access-check",
        "--target", "prod-nebius", "--image", trainer,
        *(["--preflight-image", preflight] if mode == "preflight" or flamingo else []),
        "--bundle-sha256", "b" * 64, "--step", "14084", "--attempt", str(attempt),
        *(["--schedule", "14084"] if phase == "fit" else []),
        *(["--resume", "--resume-checkpoint", "500:" + "a" * 64,
           "--graceful-stop", "--gpu-limit-bytes", "16000000000"] if restore else []),
        *(["--cached-weights-sha256", "c" * 64, "--validation-sha256", "d" * 64,
           "--direct-objective", "physical-mae"] if paired or flamingo else []),
        *(["--flamingo-binding-sha256", "1" * 64, "--flamingo-baselines", "2" * 64, "3" * 64,
           "--flamingo-parity-sha256", "4" * 64] if flamingo else []),
    ]))


def check_permissions(kube, released):
    for namespace, verb, resource, allowed in (
        ("training", "create", "jobs.batch", released),
        ("training", "get", "jobs.batch", released),
        ("training", "list", "jobs.batch", released),
        ("training", "watch", "jobs.batch", released),
        ("training", "patch", "jobs.batch", False),
        ("training", "delete", "jobs.batch", False),
        ("training", "get", "pods", released),
        ("training", "list", "pods", released),
        ("training", "get", "pods/log", released),
        ("training", "create", "pods", False),
        ("training", "create", "pods/exec", False),
        ("training", "get", "pods/exec", False),
        ("training", "create", "pods/portforward", False),
        ("training", "get", "pods/portforward", False),
        ("training", "get", "secrets", False),
        ("training", "list", "secrets", False),
        ("training", "watch", "secrets", False),
        ("training", "create", "serviceaccounts/token", False),
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
    roles = ("trainer", "preflight", "flamingo-trainer", "flamingo-preflight")
    for role in roles:
        parser.add_argument(f"--{role}-image", dest=role, help=f"Verified {role} image from the signed release")
    refs = {role: ref for role, ref in vars(parser.parse_args()).items() if ref is not None}
    released = bool(refs)
    if released:
        if set(refs) not in (set(roles[:2]), set(roles)) or not all(
                re.fullmatch(r"ghcr\.io/[^@\s]+@sha256:[0-9a-f]{64}", ref) for ref in refs.values()):
            parser.error("Supply a pair of signed registry digests for each released backbone")
        images = release_images()
        if set(refs) != set(roles).intersection(images):
            raise ValueError("Probe images must cover every released training role")
        for role, ref in refs.items():
            require_image(images, role, ref)
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
    flamingo = (refs["flamingo-trainer"], refs["flamingo-preflight"]) if "flamingo-trainer" in refs else None
    exercise_release(refs["trainer"], refs["preflight"], check, flamingo_images=flamingo)


def exercise_release(trainer_image, preflight_image, check, *, flamingo_images=None):
    invalid = "example.invalid/probe@sha256:" + "a" * 64
    for mode in ("preflight", "job"):
        trainer = invalid if mode == "job" else trainer_image
        preflight = invalid if mode == "preflight" else preflight_image
        check(render_job(mode, trainer, preflight), "unapproved-" + mode, "approved image")
        check(render_job(mode, preflight_image, trainer_image), "swapped-" + mode, "approved image")
        check(render_job(mode, trainer_image, preflight_image), mode)
        paired_job = render_job(mode, trainer_image, preflight_image, paired=True)
        check(paired_job, "paired-" + mode)
        job = deepcopy(paired_job)
        del job["spec"]["template"]["spec"]["containers"][0]["command"]
        check(job, "default-entrypoint-" + mode, "episode entrypoint")
        if flamingo_images:
            flamingo_job = render_job(mode, *flamingo_images, flamingo=True)
            check(flamingo_job, "flamingo-" + mode)
            job = deepcopy(flamingo_job)
            del job["spec"]["template"]["spec"]["containers"][0]["command"]
            check(job, "flamingo-default-entrypoint-" + mode, "approved image")
            for backbone, valid_job, (other_trainer, other_preflight) in (
                ("flamingo", flamingo_job, (trainer_image, preflight_image)),
                ("qwen", paired_job, flamingo_images),
            ):
                job = deepcopy(valid_job)
                container, = job["spec"]["template"]["spec"]["containers"]
                other_image = other_trainer if mode == "job" else other_preflight
                container["image"] = other_image
                check(job, f"{backbone}-wrong-backbone-{mode}", "approved image")

                job = deepcopy(valid_job)
                container, = job["spec"]["template"]["spec"]["containers"]
                episode_image, = [e for e in container["env"] if e["name"] == "EPISODE_IMAGE"]
                episode_image["value"] = other_trainer
                check(job, f"{backbone}-wrong-episode-image-{mode}", "approved environment")

                swapped = deepcopy(job)
                swapped["spec"]["template"]["spec"]["containers"][0]["image"] = other_image
                check(swapped, f"{backbone}-wrong-pair-{mode}", "approved image")
        else:
            check(render_job(mode, trainer_image, preflight_image, flamingo=True),
                  "flamingo-unreleased-" + mode, QWEN_ONLY["message"])
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
    ):
        job = render_job("job", trainer_image, preflight_image)
        job["spec"]["template"]["spec"][field] = value
        check(job, label, message)
    for mode, label, change in (("job", "other-claim", {"claimName": "scoring-fixture"}),
                                ("preflight", "writable-claim", {"readOnly": False}),
                                ("job", "read-only-gpu-state", {"readOnly": True})):
        job = render_job(mode, trainer_image, preflight_image)
        inputs = next(v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == "inputs")
        inputs["persistentVolumeClaim"].update(change)
        check(job, label, "one scoped training volume")
    job = render_job("job", trainer_image, preflight_image)
    pod = job["spec"]["template"]["spec"]
    pod["volumes"].append({"name": "state", "persistentVolumeClaim": {"claimName": "airaware-checkpoints"}})
    check(job, "second-mount-of-same-claim", "one scoped training volume")
    job = render_job("job", trainer_image, preflight_image)
    mount = next(m for m in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if m["mountPath"] == "/bundle")
    mount["readOnly"] = False
    check(job, "writable-bundle-subpath", "checkpoint mount paths")
    for label, subpath in (("whole-training-disk", None), ("cache-as-state", "airaware/cache"),
                           ("scorer-as-state", "scorer/confirmation")):
        job = render_job("job", trainer_image, preflight_image)
        mount = next(m for m in job["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] if m["mountPath"] == "/state")
        if subpath is None:
            del mount["subPath"]
        else:
            mount["subPath"] = subpath
        check(job, label, "checkpoint mount paths")
    job = render_job("job", trainer_image, preflight_image)
    job["spec"]["template"]["spec"]["securityContext"]["fsGroup"] = 0
    check(job, "wrong-volume-group", "training filesystem group")
    job = render_job("job", trainer_image, preflight_image)
    ca = next(v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == "tracking-ca")
    ca["configMap"]["name"] = "unapproved-ca"
    check(job, "substituted-tracking-ca", "the tracking CA")
    job = render_job("job", trainer_image, preflight_image)
    job["spec"]["template"]["spec"]["containers"][0]["env"].append({
        "name": "FIXTURE", "valueFrom": {"secretKeyRef": {"name": "scoring-fixture", "key": "marker"}},
    })
    check(job, "unapproved-secret-reference", "training tracking URI")


if __name__ == "__main__":
    main()
