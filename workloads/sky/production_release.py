"""Verify the approved Nebius image release on the operator's management host."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
TRUST = Path("/etc/ml-infra/nebius-release")
ROOTLESS = "ghcr.io/keneth-arias/ml-infra-sky-cpu"
PAIRED = "ghcr.io/keneth-arias/ml-infra-qwen"
ROLES = ("rootless-cpu", "trainer", "preflight")


def release_refs(release):
    if release["schema"] != "nebius-image-release-v1":
        raise ValueError("Unexpected image release format")
    cpu, pair = release["rootless"], release["paired"]
    if cpu["role"] != "rootless-cpu" or pair["schema"] != "paired-forecast-candidate-v1":
        raise ValueError("Unexpected candidate role or format")
    if set(pair["images"]) != {"trainer", "preflight"}:
        raise ValueError("Expected exactly the trainer and preflight roles")
    for commit in (cpu["build_commit"], pair["build_commit"], pair["forecast_commit"], pair["reader_commit"]):
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or commit == "0" * 40:
            raise ValueError("Expected immutable candidate source commits")
    refs = {}
    for role, record in {"rootless-cpu": cpu, **pair["images"]}.items():
        expected_repo = ROOTLESS if role == "rootless-cpu" else PAIRED
        if record["repository"] != expected_repo or not all(
            re.fullmatch(r"sha256:[0-9a-f]{64}", record[k]) for k in ("index", "linux_amd64", "config")
        ):
            raise ValueError(f"Unexpected repository or OCI digest for {role}")
        refs[role] = f"{expected_repo}@{record['index']}"
    if refs["trainer"] == refs["preflight"]:
        raise ValueError("Trainer and preflight must use distinct OCI indexes")
    return refs


def signed_bytes(directory, name, signer):
    raw = (directory / name).read_bytes()
    # Parse the same bytes that OpenSSL verified, even during a release switch.
    result = subprocess.run(
        ["/usr/bin/openssl", "dgst", "-sha256", "-verify", str(signer),
         "-signature", str(directory / (name + ".sig"))],
        input=raw, capture_output=True, timeout=10,
    )
    if result.returncode:
        raise ValueError(f"Untrusted signature for {name}")
    return raw


def verify_release(trust, cluster_id, deployment_commit):
    directory = (trust / "current").resolve(strict=True)
    signer = trust / "signer.pub"
    approval = signed_bytes(directory, "deployment.json", signer)
    release = signed_bytes(directory, "release.json", signer)
    deploy = json.loads(approval)
    if (
        deploy["schema"] != "nebius-image-deployment-v1"
        or deploy["cluster_id"] != cluster_id
        or deploy["deployment_commit"] != deployment_commit
        or deploy["release_sha256"] != sha256(release).hexdigest()
    ):
        raise ValueError("Deployment receipt differs from the cluster, checkout or image release")
    return release_refs(json.loads(release)), sha256(approval).hexdigest()


def current_release():
    # Resolve the checkout that supplied this launcher, independently of the caller's cwd.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git = ["/usr/bin/git", "--no-replace-objects", "-C", str(REPO)]
    commit = subprocess.check_output([*git, "rev-parse", "--verify", "HEAD"], env=env, text=True).strip()
    subprocess.run([*git, "diff", "--quiet", "HEAD", "--", "workloads", "bootstrap/nebius"],
                   env=env, check=True)
    return verify_release(TRUST, os.environ["NEBIUS_TRAINING_CLUSTER_ID"], commit)


def release_images():
    return current_release()[0]


def require_image(images, role, ref):
    if ref != images[role]:
        raise ValueError(f"Image differs from the approved {role} role")


def single_container(pod):
    if len(pod["containers"]) != 1 or pod.get("initContainers") or pod.get("ephemeralContainers"):
        raise ValueError("Expected one release container")
    return pod["containers"][0]


def check_episode(images, preflight, task):
    if preflight.get("kind") != "Job":
        raise ValueError("Expected the rendered preflight Job")
    validator = single_container(preflight["spec"]["template"]["spec"])
    require_image(images, "preflight", validator["image"])
    bound = [e.get("value") for e in validator["env"] if e["name"] == "EPISODE_IMAGE"]
    if bound != [images["trainer"]]:
        raise ValueError("Preflight does not identify the released trainer")
    if task.get("kind") == "Job":
        container = single_container(task["spec"]["template"]["spec"])
        require_image(images, "trainer", container["image"])
    else:
        resources = task["resources"]
        if resources["infra"] != "k8s/prod-nebius":
            raise ValueError("Expected the managed SkyPilot target")
        container = single_container(task["config"]["kubernetes"]["pod_config"]["spec"])
        require_image(images, "trainer", resources["image_id"].removeprefix("docker:"))
        if "image" in container:
            require_image(images, "trainer", container["image"])
        require_image(images, "trainer", task["envs"]["EPISODE_IMAGE"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("manifest", help="Assemble unsigned release bytes from the two candidate receipts")
    build.add_argument("rootless", type=Path)
    build.add_argument("paired", type=Path)
    commands.add_parser("reference", help="Print one verified production reference").add_argument("role", choices=ROLES)
    episode = commands.add_parser("check-episode")
    episode.add_argument("preflight", type=Path)
    episode.add_argument("task", type=Path)
    args = parser.parse_args()
    if args.command == "manifest":
        release = {"schema": "nebius-image-release-v1", "rootless": json.loads(args.rootless.read_bytes()),
                   "paired": json.loads(args.paired.read_bytes())}
        release_refs(release)
        print(json.dumps(release, sort_keys=True, indent=2))
    else:
        images, fingerprint = current_release()
        if args.command == "reference":
            print(images[args.role])
        else:
            check_episode(images, json.loads(args.preflight.read_bytes()), json.loads(args.task.read_bytes()))
            print(fingerprint)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        sys.exit(f"Production release verification failed: {exc}")
