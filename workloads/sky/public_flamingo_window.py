"""Verify one signed successor storage handoff against its allocation and installed release."""

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import uuid

import production_release as release


SCHEMA = "public-flamingo-stage-window-v1"
CLUSTER = "mk8scluster-e07hgffmypr4331sn2"
PROJECT = "project-e07z8wzama002x047kpx72"
CA = "122a800e233ecae05a71701c057a0f77d786b5c7e357e9cb93b6dfff0b5b301e"
NAMESPACE_UID = "40fd2338-8bfe-414c-b97f-ddbecbfde98b"
ORIGINAL_SCOPE_SHA = "a82b7ef7bd96295b9ce33fa10d629be2e19b1c9f8dd0b55f0c513921804be7f6"
FIXED = {
    "cluster_id": CLUSTER, "project_id": PROJECT, "ca_sha256": CA,
    "namespace": "training", "namespace_uid": NAMESPACE_UID,
    "binding_sha256": "55c3fc877fd6e0cce829a66e5b20cb061a02bb60d3078d01411b24a7789ef653",
    "promotion_sha256": "71e27185948aa31776654decf9edd55351a753d33985259ce1cd9d1555845b6c",
    "objects_sha256": "f493bb7ae333e1efda40ff5a6c2a98beb6e64528847c8c49663c3bddf9a3293f",
    "weights_manifest_sha256": "7f8562992f1cf3852ced581f331247c6420fc90292d45453424c77f0a29c199f",
}


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z", value):
        raise ValueError("Expected a signed UTC timestamp ending in Z")
    return datetime.fromisoformat(value.removesuffix("Z") + "+00:00")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field in signed window or allocation scope")
        result[key] = value
    return result


def private_scope(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Allocation scope must be a private regular file")
        raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Oversized allocation scope")
        return raw


def eligibility_label(run):
    return "airaware.training.node-restriction.kubernetes.io/" + run


@dataclass(frozen=True)
class Window:
    sha256: str
    run: str
    allocation_run_id: str
    scope_sha256: str
    start: str
    cutoff: str
    cluster_id: str
    project_id: str
    ca_sha256: str
    namespace: str
    namespace_uid: str
    deployment_commit: str
    deployment_sha256: str
    preflight_image: str
    preflight_platform_image: str
    cpu_node: str
    cpu_provider_id: str
    cpu_node_uid: str
    binding_sha256: str
    promotion_sha256: str
    objects_sha256: str
    weights_manifest_sha256: str
    _scope_bytes: bytes = field(repr=False)

    def scope_data(self):
        return json.loads(self._scope_bytes, object_pairs_hook=unique_object)

    def validate_namespace(self, namespace):
        meta = namespace["metadata"]
        if (namespace.get("kind") != "Namespace" or meta.get("name") != self.namespace
                or meta.get("uid") != self.namespace_uid or meta.get("deletionTimestamp")):
            raise ValueError("Expected the enrolled retained storage namespace UID")

    def validate_placement(self, node):
        meta, spec, status = node["metadata"], node["spec"], node["status"]
        labels = meta["labels"]
        if (meta["name"] != self.cpu_node or meta["uid"] != self.cpu_node_uid or meta.get("deletionTimestamp")
                or spec.get("providerID") != self.cpu_provider_id or spec.get("unschedulable")
                or labels.get(eligibility_label(self.run)) != self.cpu_node_uid
                or labels.get("airaware.training/pool") != "training-cpu"
                or labels.get("kubernetes.io/arch") != "amd64" or labels.get("kubernetes.io/os") != "linux"
                or status.get("allocatable", {}).get("nvidia.com/gpu", "0") != "0"
                or not any(c["type"] == "Ready" and c["status"] == "True" for c in status["conditions"])):
            raise ValueError("Expected the signed Ready Linux amd64 CPU worker identity and window eligibility label")


def add_window_args(parser):
    parser.add_argument("--window", type=Path, help="Signed successor JSON with adjacent .sig")
    parser.add_argument("--window-sha256", help="Independent approval pin of the signed JSON bytes")
    parser.add_argument("--window-scope", type=Path, help="Private signed-hash allocation scope (or use --scope)")


def validate_window_run(window, run):
    if window is None:
        if isinstance(run, str) and re.fullmatch("fsw-[0-9a-f]{32}", run):
            raise ValueError("Successor run names require a signed window")
    elif run != window.run:
        raise ValueError("Run must match the content-derived signed window name")


def validate_window_pin(window, approval):
    if window is None:
        if "window_sha256" in approval:
            raise ValueError("A window-bound approval requires successor mode")
    elif approval.get("window_sha256") != window.sha256:
        raise ValueError("Approval does not identify the exact signed successor window")


def check_deployment(window):
    manifest, fingerprint = release.verified_release_manifest(release.TRUST, window.cluster_id, window.deployment_commit)
    # Apply the complete installed format's validator. Public staging runs the paired CPU preflight.
    roles = release.release_refs(manifest)
    preflight = manifest["paired"]["images"]["preflight"]
    platform_image = preflight["repository"] + "@" + preflight["linux_amd64"]
    if (fingerprint != window.deployment_sha256 or roles["preflight"] != window.preflight_image
            or platform_image != window.preflight_platform_image):
        raise ValueError("Signed window differs from the installed release's paired CPU preflight")


def verify_live_window(window):
    if window is None:
        return
    if (os.environ.get("NEBIUS_TRAINING_CLUSTER_ID") != window.cluster_id
            or os.environ.get("NEBIUS_CA_BUNDLE_SHA256") != window.ca_sha256):
        raise ValueError("Managed cluster and CA pins differ from the signed successor window")
    if not timestamp(window.start) <= utcnow() < timestamp(window.cutoff):
        raise ValueError("Successor storage handoff is outside its signed allocation interval")
    check_deployment(window)


def load_window(args, run, *, live=False):
    explicit_scope = args.window_scope
    if args.window is None and args.window_sha256 is None and explicit_scope is None:
        validate_window_run(None, run)
        return None
    pin = args.window_sha256
    path_to_scope = explicit_scope or getattr(args, "scope", None)
    if not (args.window and path_to_scope and isinstance(pin, str) and re.fullmatch("[0-9a-f]{64}", pin)):
        raise ValueError("Successor mode requires a signed window, independent SHA-256 and allocation scope")
    path = Path(args.window)
    raw = release.signed_bytes(path.parent, path.name, release.TRUST / "signer.pub")
    if sha256(raw).hexdigest() != pin:
        raise ValueError("Window bytes differ from their independent approval pin")
    data = json.loads(raw, object_pairs_hook=unique_object)
    expected = {f.name for f in fields(Window)} - {"sha256", "run", "_scope_bytes"}
    if (not isinstance(data, dict) or data.pop("schema", None) != SCHEMA or set(data) != expected
            or not all(isinstance(value, str) for value in data.values())
            or any(data[key] != value for key, value in FIXED.items())):
        raise ValueError("Unexpected successor schema, target or retained public inputs")
    if (not re.fullmatch("[0-9a-f]{40}", data["deployment_commit"]) or data["deployment_commit"] == "0"*40
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", data["allocation_run_id"])
            or not re.fullmatch("computeinstance-[a-z0-9]+", data["cpu_node"])
            or data["cpu_provider_id"] != "nebius://" + data["cpu_node"]
            or str(uuid.UUID(data["cpu_node_uid"])) != data["cpu_node_uid"] or not uuid.UUID(data["cpu_node_uid"]).int
            or timestamp(data["start"]) >= timestamp(data["cutoff"])):
        raise ValueError("Invalid allocation, release, interval or managed CPU identity")

    scope_bytes = private_scope(path_to_scope)
    operational_scope = getattr(args, "scope", None)
    if operational_scope and Path(operational_scope) != Path(path_to_scope) and private_scope(operational_scope) != scope_bytes:
        raise ValueError("Command scope differs from signed window scope")
    if sha256(scope_bytes).hexdigest() != data["scope_sha256"] or data["scope_sha256"] == ORIGINAL_SCOPE_SHA:
        raise ValueError("Expected the independently pinned successor allocation scope")
    window = Window(sha256=pin, run="fsw-" + pin[:32], _scope_bytes=scope_bytes, **data)
    scope = window.scope_data()
    scope_fields = {"run_id": window.allocation_run_id, "cluster_id": window.cluster_id, "project_id": window.project_id,
                    "start": window.start, "cutoff": window.cutoff, "support_instance_id": window.cpu_node}
    if scope.get("stage") not in {"support", "gpu"} or any(scope.get(k) != v for k, v in scope_fields.items()):
        raise ValueError("Window does not identify the exact allocation and its support worker")
    if (window.allocation_run_id == "support-20261121d"
            and timestamp(window.start) == timestamp("2026-11-21T22:27:06Z")
            and timestamp(window.cutoff) == timestamp("2026-11-22T02:25:00Z")):
        raise ValueError("The original allocation must use its enrolled handoff")
    validate_window_run(window, run)
    if live:
        verify_live_window(window)
    else:
        check_deployment(window)
    return window
