#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Render a bounded production Flux handoff from a reviewed merged HAMi revision."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[4]
SYSTEM = "hami-system"
FLUX = "flux-system"
NAME = "nebius-hami"
PATH = "platform/hami/system"
REPO = "ssh://git@github.com/keneth-arias/ml-infra.git"
RBAC = "rbac.authorization.k8s.io/v1"
TARGETS = {
    ("v1", "ConfigMap"): ("", "configmaps"),
    ("v1", "Service"): ("", "services"),
    ("v1", "ServiceAccount"): ("", "serviceaccounts"),
    ("apps/v1", "DaemonSet"): ("apps", "daemonsets"),
    ("apps/v1", "Deployment"): ("apps", "deployments"),
    ("networking.k8s.io/v1", "NetworkPolicy"): ("networking.k8s.io", "networkpolicies"),
    ("policy/v1", "PodDisruptionBudget"): ("policy", "poddisruptionbudgets"),
}


def object_(api, kind, name, namespace, **body):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": name, "namespace": namespace}, **body}


def sync(commit, fetch):
    source_name = f"hami-{commit}"
    source = object_("source.toolkit.fluxcd.io/v1", "GitRepository", source_name, FLUX, spec={
        "url": REPO, "interval": "10m", "ref": {"commit": commit},
        "secretRef": {"name": "nebius-git-read"}, "suspend": not fetch, "timeout": "60s",
    })
    reconcile = object_("kustomize.toolkit.fluxcd.io/v1", "Kustomization", NAME, FLUX, spec={
        "sourceRef": {"kind": "GitRepository", "name": source_name},
        "path": f"./{PATH}", "interval": "5m", "timeout": "5m",
        "targetNamespace": SYSTEM, "serviceAccountName": NAME,
        "prune": False, "deletionPolicy": "Orphan", "force": False, "suspend": True, "wait": False,
    })
    return source, reconcile


def rule(group, resources, names, verbs):
    return {"apiGroups": [group], "resources": resources, "resourceNames": names, "verbs": verbs}


def binding(name, namespace, account):
    return object_(RBAC, "RoleBinding", name, namespace,
                   roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                   subjects=[{"kind": "ServiceAccount", "name": account, "namespace": FLUX}])


def access(objects):
    names = {}
    for obj in objects:
        metadata = obj["metadata"]
        target = TARGETS.get((obj["apiVersion"], obj["kind"]))
        if target is None or metadata.get("namespace") != SYSTEM or not metadata.get("name"):
            raise ValueError("HAMi core contains an object outside the namespaced Flux grant")
        group = names.setdefault(target, set())
        if metadata["name"] in group:
            raise ValueError("HAMi core has a duplicate object")
        group.add(metadata["name"])
    if not names:
        raise ValueError("HAMi core is empty")
    controller = NAME + "-controller"
    return [
        object_("v1", "ServiceAccount", NAME, FLUX, automountServiceAccountToken=False),
        object_(RBAC, "Role", controller, FLUX, rules=[
            rule("kustomize.toolkit.fluxcd.io",
                 ["kustomizations", "kustomizations/status", "kustomizations/finalizers"],
                 [NAME], ["patch", "update"]),
            rule("", ["serviceaccounts"], [NAME], ["get", "impersonate"]),
        ]),
        binding(controller, FLUX, "kustomize-controller"),
        object_(RBAC, "Role", NAME, SYSTEM, rules=[
            rule(group, [resource], sorted(members), ["get", "patch"])
            for (group, resource), members in sorted(names.items())
        ]),
        binding(NAME, SYSTEM, NAME),
    ]


def bootstrap_core(objects):
    access(objects)
    staged = deepcopy(objects)
    schedulers = [obj for obj in staged if obj["kind"] == "Deployment"
                  and obj["metadata"]["name"] == "hami-scheduler"]
    if len(schedulers) != 1 or sum(obj["kind"] == "Deployment" for obj in staged) != 1:
        raise ValueError("Expected the single reviewed HAMi scheduler Deployment")
    schedulers[0]["spec"]["replicas"] = 0
    return staged


def activate(commit, observed_source, observed_stage):
    wanted_source, wanted_stage = sync(commit, True)
    for observed, wanted in ((observed_source, wanted_source), (observed_stage, wanted_stage)):
        if (any(observed.get(key) != wanted[key] for key in ("kind", "apiVersion"))
                or any(observed["metadata"].get(key) != val for key, val in wanted["metadata"].items())
                or not observed["metadata"].get("uid") or observed["metadata"].get("deletionTimestamp")):
            raise ValueError("Unexpected live HAMi Flux object identity")
    source_spec = deepcopy(observed_source["spec"])
    source_spec.setdefault("suspend", False)
    if source_spec != wanted_source["spec"]:
        raise ValueError("Live HAMi source differs from the exact commit-pinned source")
    stage_spec = deepcopy(observed_stage["spec"])
    stage_spec.setdefault("wait", False)
    stage_spec.setdefault("force", False)
    if stage_spec != wanted_stage["spec"]:
        raise ValueError("Live HAMi Kustomization differs from the exact suspended stage")
    status = observed_source["status"]
    generation = observed_source["metadata"]["generation"]
    revision = status["artifact"]["revision"]
    if (status["observedGeneration"] != generation or revision.split("@")[-1] != f"sha1:{commit}"
            or not any(c["type"] == "Ready" and c["status"] == "True" and c["observedGeneration"] == generation
                       for c in status["conditions"])):
        raise ValueError("HAMi source has no observed Ready artifact for the exact commit")
    if not observed_stage["metadata"].get("resourceVersion"):
        raise ValueError("Expected the live suspended object's resourceVersion")
    wanted_stage["metadata"]["resourceVersion"] = observed_stage["metadata"]["resourceVersion"]
    wanted_stage["spec"]["suspend"] = False
    return wanted_stage


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def merged(commit):
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or commit == "0" * 40:
        raise ValueError("Supply the full immutable HAMi commit")
    git("merge-base", "--is-ancestor", commit, "refs/remotes/origin/main")
    if git("cat-file", "-t", f"{commit}:{PATH}/kustomization.yaml").strip() != b"blob":
        raise ValueError("Merged commit does not contain the HAMi system kustomization")


def core(commit):
    import yaml

    archive = git("archive", commit, PATH)
    with tempfile.TemporaryDirectory(prefix="hami-flux-") as checkout:
        subprocess.run(["tar", "-xf", "-", "-C", checkout], input=archive, check=True)
        raw = subprocess.check_output(["kubectl", "kustomize", str(Path(checkout) / PATH)])
    return [obj for obj in yaml.safe_load_all(raw) if obj is not None]


def item_list(objects):
    return {"apiVersion": "v1", "kind": "List", "items": objects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commit")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("access", help="Render patch-only grants from the exact merged build")
    commands.add_parser("core", help="Render the exact merged HAMi core as a Kubernetes List")
    commands.add_parser("bootstrap-core", help="Render the core with zero scheduler replicas for first creation")
    stage = commands.add_parser("stage", help="Stage suspended or enable only Git fetch")
    stage.add_argument("--fetch", action="store_true")
    enable = commands.add_parser("enable", help="Bind activation to verified live readbacks")
    enable.add_argument("--source-readback", type=Path, required=True)
    enable.add_argument("--kustomization-readback", type=Path, required=True)
    args = parser.parse_args()
    try:
        merged(args.commit)
        if args.command == "access":
            result = item_list(access(core(args.commit)))
        elif args.command == "core":
            objects = core(args.commit)
            access(objects)
            result = item_list(objects)
        elif args.command == "bootstrap-core":
            result = item_list(bootstrap_core(core(args.commit)))
        elif args.command == "stage":
            result = item_list(sync(args.commit, args.fetch))
        else:
            result = activate(args.commit, json.loads(args.source_readback.read_bytes()),
                              json.loads(args.kustomization_readback.read_bytes()))
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
