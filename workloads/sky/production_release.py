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
FLAMINGO_ROLES = ("flamingo-trainer", "flamingo-preflight")
ROLE_VARIABLES = {"trainer": "trainerImage", "preflight": "preflightImage",
                  "flamingo-trainer": "flamingoTrainerImage", "flamingo-preflight": "flamingoPreflightImage"}
QWEN_ONLY = {
    "expression": "!variables.flamingo",
    "message": "Flamingo Jobs require a signed dual-backbone release.",
}
QWEN_IMAGE_SELECTORS = {"fitImage": "variables.trainerImage", "checkImage": "variables.preflightImage"}
QWEN_ONLY_VARIABLES = {
    "argv": "has(variables.container.command) ? variables.container.command : []",
    "flamingo": "'--flamingo-binding-sha256' in variables.argv",
    **QWEN_IMAGE_SELECTORS,
}


def model_roles(command):
    return FLAMINGO_ROLES if "--flamingo-binding-sha256" in command else ("trainer", "preflight")


def release_refs(release):
    dual = release["schema"] == "nebius-image-release-v2"
    if (not dual and release["schema"] != "nebius-image-release-v1") or ("flamingo" in release) != dual:
        raise ValueError("Unexpected image release format or backbone roles")
    cpu = release["rootless"]
    if cpu["role"] != "rootless-cpu":
        raise ValueError("Unexpected rootless candidate role")
    records = {"rootless-cpu": cpu}
    commits = [cpu["build_commit"]]
    pairs = [("paired", "")]
    if dual:
        pairs.append(("flamingo", "flamingo-"))
    for key, prefix in pairs:
        pair = release[key]
        if pair["schema"] != "paired-forecast-candidate-v1" or set(pair["images"]) != {"trainer", "preflight"}:
            raise ValueError("Expected a paired candidate with exactly the trainer and preflight roles")
        commits.extend(pair[k] for k in ("build_commit", "forecast_commit", "reader_commit"))
        records.update({prefix + role: record for role, record in pair["images"].items()})
    if any(not re.fullmatch(r"[0-9a-f]{40}", commit) or commit == "0" * 40 for commit in commits):
        raise ValueError("Expected immutable candidate source commits")
    refs = {}
    for role, record in records.items():
        expected_repo = ROOTLESS if role == "rootless-cpu" else PAIRED
        if record["repository"] != expected_repo or not all(
            re.fullmatch(r"sha256:[0-9a-f]{64}", record[k]) for k in ("index", "linux_amd64", "config")
        ):
            raise ValueError(f"Unexpected repository or OCI digest for {role}")
        refs[role] = f"{expected_repo}@{record['index']}"
    if len(set(refs.values())) != len(refs):
        raise ValueError("All backbone and preflight roles must use distinct OCI indexes")
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


def verified_release_manifest(trust, cluster_id, deployment_commit):
    """Read the release and deployment signed for the same installed directory."""
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
    return json.loads(release), sha256(approval).hexdigest()


def verify_release(trust, cluster_id, deployment_commit):
    release, fingerprint = verified_release_manifest(trust, cluster_id, deployment_commit)
    return release_refs(release), fingerprint


def current_release():
    # Resolve the checkout that supplied this launcher, independently of the caller's cwd.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    git = ["/usr/bin/git", "--no-replace-objects", "-C", str(REPO)]
    commit = subprocess.check_output([*git, "rev-parse", "--verify", "HEAD"], env=env, text=True).strip()
    subprocess.run([*git, "diff", "--quiet", "HEAD", "--", "workloads", "bootstrap/nebius", "platform/overlays/prod", "clusters/prod-nebius"],
                   env=env, check=True)
    return verify_release(TRUST, os.environ["NEBIUS_TRAINING_CLUSTER_ID"], commit)


def release_images():
    return current_release()[0]


def require_image(images, role, ref):
    if role not in images or ref != images[role]:
        raise ValueError(f"Image differs from the approved {role} role")


def single_container(pod):
    if len(pod["containers"]) != 1 or pod.get("initContainers") or pod.get("ephemeralContainers"):
        raise ValueError("Expected one release container")
    return pod["containers"][0]


def check_native_policy(images, policy, *, allow_held_candidates=False):
    if (policy.get("kind"), policy.get("metadata", {}).get("name")) != (
        "ValidatingAdmissionPolicy", "episode-submit-inputs"
    ):
        raise ValueError("Expected the native training admission policy")
    spec = policy["spec"]
    flamingo_roles = images.keys() & set(FLAMINGO_ROLES)
    if flamingo_roles and flamingo_roles != set(FLAMINGO_ROLES):
        raise ValueError("Expected both signed Flamingo roles")
    checks = spec.get("validations", [])
    held_qwen_release = (allow_held_candidates and not flamingo_roles and checks
                         and checks[0]["expression"] == "false")

    def expressions(name):
        return [v["expression"] for v in spec["variables"] if v["name"] == name]

    for role, variable in ROLE_VARIABLES.items():
        if held_qwen_release and role in FLAMINGO_ROLES:
            continue
        expected = [repr(images[role])] if role in images else []
        if role not in FLAMINGO_ROLES and not expected:
            raise ValueError(f"Missing signed {role} image")
        if expressions(variable) != expected:
            raise ValueError(f"Native admission policy differs from the approved {role} image")
    if not flamingo_roles and not held_qwen_release and (
        checks.count(QWEN_ONLY) != 1
        or any(expressions(name) != [expression] for name, expression in QWEN_ONLY_VARIABLES.items())
    ):
        raise ValueError("A v1 native admission policy requires the explicit Flamingo denial and Qwen image selectors")


def check_trainer_env(container, trainer):
    bound = [e.get("value") for e in container["env"] if e["name"] == "EPISODE_IMAGE"]
    if bound != [trainer]:
        raise ValueError("Job does not identify the released trainer")


def check_episode(images, preflight, task):
    if preflight.get("kind") != "Job":
        raise ValueError("Expected the rendered preflight Job")
    validator = single_container(preflight["spec"]["template"]["spec"])
    train_role, pre_role = model_roles(validator["command"])
    require_image(images, pre_role, validator["image"])
    check_trainer_env(validator, images[train_role])
    if task.get("kind") == "Job":
        container = single_container(task["spec"]["template"]["spec"])
        if model_roles(container["command"]) != (train_role, pre_role):
            raise ValueError("Preflight and fit must use the same backbone")
        require_image(images, train_role, container["image"])
        check_trainer_env(container, images[train_role])
    else:
        if train_role != "trainer":
            raise ValueError("Flamingo requires a native managed Job")
        resources = task["resources"]
        if resources["infra"] != "k8s/prod-nebius":
            raise ValueError("Expected the managed SkyPilot target")
        container = single_container(task["config"]["kubernetes"]["pod_config"]["spec"])
        if resources["image_id"] != "docker:" + images["trainer"]:
            raise ValueError("Image differs from the approved trainer role")
        if "image" in container:
            require_image(images, "trainer", container["image"])
        require_image(images, "trainer", task["envs"]["EPISODE_IMAGE"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("manifest", help="Assemble unsigned release bytes from the two candidate receipts")
    build.add_argument("rootless", type=Path)
    build.add_argument("paired", type=Path, help="Approved Qwen trainer and preflight candidate")
    build.add_argument("--flamingo", type=Path, help="Additional approved Flamingo trainer and preflight candidate")
    commands.add_parser("reference", help="Print one verified production reference").add_argument("role", choices=ROLES + FLAMINGO_ROLES)
    commands.add_parser("check-native-policy", help="Compare policy JSON on stdin with the signed role images")
    episode = commands.add_parser("check-episode")
    episode.add_argument("preflight", type=Path)
    episode.add_argument("task", type=Path)
    args = parser.parse_args()
    if args.command == "manifest":
        release = {"schema": "nebius-image-release-v1", "rootless": json.loads(args.rootless.read_bytes()),
                   "paired": json.loads(args.paired.read_bytes())}
        if args.flamingo:
            release["schema"] = "nebius-image-release-v2"
            release["flamingo"] = json.loads(args.flamingo.read_bytes())
        release_refs(release)
        print(json.dumps(release, sort_keys=True, indent=2))
    else:
        images, fingerprint = current_release()
        if args.command == "reference":
            print(images[args.role])
        elif args.command == "check-native-policy":
            check_native_policy(images, json.load(sys.stdin), allow_held_candidates=True)
            print(fingerprint)
        else:
            check_episode(images, json.loads(args.preflight.read_bytes()), json.loads(args.task.read_bytes()))
            print(fingerprint)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        sys.exit(f"Production release verification failed: {exc}")
