"""Exercise managed API admission as the short-lived native training submitter."""

import json
from pathlib import Path
import subprocess
import sys

SKY = Path(__file__).resolve().parents[1] / "sky"
sys.path.insert(0, str(SKY))
from production_target import kubectl_client


def main():
    kube = kubectl_client("training", "episode-submit", direct=True)
    for namespace, verb, resource, allowed in (
        ("training", "create", "jobs.batch", True),
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

    def render(mode, *, paired=False):
        return json.loads(subprocess.check_output([
            sys.executable, SKY / "episode-profile.py", mode, "fit", "direct", "access-check",
            "--target", "prod-nebius", "--image", "example.invalid/probe@sha256:" + "a" * 64,
            "--bundle-sha256", "b" * 64, "--schedule", "14084", "--step", "14084",
            *(["--cached-weights-sha256", "c" * 64, "--validation-sha256", "d" * 64,
               "--direct-objective", "physical-mae"] if paired else []),
        ]))

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

    for mode in ("preflight", "job"):
        check(render(mode), mode)
        check(render(mode, paired=True), "paired-" + mode)
    for label, field, value, message in (
        ("other-account", "serviceAccountName", "default", "token-free"),
        ("mounted-token", "automountServiceAccountToken", True, "token-free"),
        ("other-claim", "volumes", [{"name": "inputs", "persistentVolumeClaim": {"claimName": "scoring-fixture"}},
                                   {"name": "state", "emptyDir": {}}], "read-only training inputs"),
        ("writable-input", "volumes", [{"name": "inputs", "persistentVolumeClaim": {"claimName": "airaware-checkpoints"}},
                                      {"name": "state", "emptyDir": {}}], "read-only training inputs"),
    ):
        job = render("job")
        job["spec"]["template"]["spec"][field] = value
        check(job, label, message)
    job = render("job")
    job["spec"]["template"]["spec"]["containers"][0]["env"].append({
        "name": "FIXTURE", "valueFrom": {"secretKeyRef": {"name": "scoring-fixture", "key": "marker"}},
    })
    check(job, "unapproved-secret-reference", "training tracking URI")


if __name__ == "__main__":
    main()
