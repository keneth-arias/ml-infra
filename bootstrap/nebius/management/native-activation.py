# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Render the signed, operator-only native Job activation. All output is local."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import runpy
import signal
import subprocess
import sys
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "workloads/sky"))
from private_api import api_host, pinned_kubeconfig_ca
from production_release import check_native_policy, current_release
sys.path.insert(0, str(ROOT / "bootstrap/nebius"))
from worker_plan import timestamp

NAME = "episode-submit-inputs"
ROLE = "episode-native-window"
RECEIPT = "airaware.training/deployment-sha256"
STAGE = "airaware.training/native-window"
ATTEMPT = "airaware.training/native-attempt"
FENCE = "airaware.training/native-close-fence"
IDENTITY = "system:serviceaccount:training:episode-submit"
RBAC = "/apis/rbac.authorization.k8s.io/v1/namespaces/training"
POLICY = f"validatingadmissionpolicy/{NAME}"
GUARD = f"validatingadmissionpolicybinding/{NAME}"
SESSION_HELPERS = runpy.run_path(str(Path(__file__).with_name("cpu-session.py")))
write_private = SESSION_HELPERS["write_private"]
wait_unauthorized = SESSION_HELPERS["wait_unauthorized"]
HOLD = {"expression": "false", "message": "Native training submission is closed during CPU qualification."}
SUPPORT = {
    "expression": "!variables.usesGPU || (has(request.dryRun) && request.dryRun)",
    "message": "The support-only window permits only CPU preflight Job creation.",
}


def held_policy_and_binding():
    output = subprocess.check_output(
        ["kubectl", "kustomize", str(ROOT / "platform/overlays/prod")], text=True, timeout=20,
    )
    objects = {
        obj["kind"]: obj for obj in yaml.safe_load_all(output)
        if obj["metadata"]["name"] == NAME
    }
    policy = objects["ValidatingAdmissionPolicy"]
    binding = objects["ValidatingAdmissionPolicyBinding"]
    checks = policy["spec"]["validations"]
    if (not checks or checks[0] != HOLD or checks.count(HOLD) != 1
            or policy["spec"].get("failurePolicy") != "Fail"
            or binding["spec"] != {
                "policyName": NAME, "validationActions": ["Deny"],
                "matchResources": {"namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "training"}
                }},
            }):
        raise ValueError("Expected the default native submission hold and Deny binding")
    return policy, binding


def active_policy(images, receipt, stage):
    if stage not in ("support", "gpu") or not re.fullmatch("[0-9a-f]{64}", receipt):
        raise ValueError("Expected a verified deployment digest and explicitly selected window stage")
    policy, _ = held_policy_and_binding()
    check_native_policy(images, policy)
    policy["metadata"]["annotations"] = {RECEIPT: receipt, STAGE: stage}
    checks = policy["spec"]["validations"]
    policy["spec"]["validations"] = ([SUPPORT] if stage == "support" else []) + checks[1:]
    return policy


def with_match_defaults(spec, key):
    # The API supplies these defaults without changing which requests match.
    result = dict(spec)
    match = dict(result[key])
    match.setdefault("matchPolicy", "Equivalent")
    match.setdefault("namespaceSelector", {})
    match.setdefault("objectSelector", {})
    for name in ("resourceRules", "excludeResourceRules"):
        if name in match:
            match[name] = [{"scope": "*", **rule} for rule in match[name]]
    result[key] = match
    return result


def check_active(policy, binding, live):
    supplied = live["items"]
    expected = {
        (obj["apiVersion"], obj["kind"], obj["metadata"]["name"]): obj
        for obj in (policy, binding)
    }
    if len(supplied) != len(expected):
        raise ValueError("Expected only the live native policy and its binding")
    for observed in supplied:
        identity = (observed["apiVersion"], observed["kind"], observed["metadata"]["name"])
        target = expected.pop(identity, None)
        if target is None:
            raise ValueError("Unexpected live native policy or binding identity")
        is_policy = observed["kind"] == "ValidatingAdmissionPolicy"
        match_key = "matchConstraints" if is_policy else "matchResources"
        if with_match_defaults(observed["spec"], match_key) != with_match_defaults(target["spec"], match_key):
            raise ValueError("Live native policy or Deny binding differs from the reviewed object")
        if is_policy:
            annotations = observed["metadata"].get("annotations", {})
            intended = policy["metadata"].get("annotations", {})
            if any(annotations.get(key) != intended.get(key) for key in (RECEIPT, STAGE, ATTEMPT)):
                raise ValueError("Live native policy is not annotated for this deployment and stage")
            generation = observed["metadata"].get("generation", 0)
            status = observed.get("status", {})
            checking = status.get("typeChecking")
            if (generation < 1 or status.get("observedGeneration") != generation
                    or not isinstance(checking, dict) or checking.get("expressionWarnings")):
                raise ValueError("Live native policy lacks current successful CEL type checking")


def access_objects(receipt):
    resource = {"apiVersion": "rbac.authorization.k8s.io/v1",
                "metadata": {"name": ROLE, "namespace": "training", "annotations": {RECEIPT: receipt}}}
    role = {**resource, "kind": "Role", "rules": [
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]}
    binding = {
        **resource, "kind": "RoleBinding", "metadata": dict(resource["metadata"]),
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ROLE},
        "subjects": [{"kind": "ServiceAccount", "name": "episode-submit", "namespace": "training"}],
    }
    return {"apiVersion": "v1", "kind": "List", "items": [role, binding]}


def require_window(scope_file, stage, cluster, now):
    mode = scope_file.stat()
    if mode.st_mode & 0o077 or mode.st_uid != os.getuid():
        raise ValueError("Expected an owner-only approved window scope")
    scope = json.loads(scope_file.read_bytes())
    start, cutoff = timestamp(scope["start"]), timestamp(scope["cutoff"])
    if (scope["stage"] != stage or scope["cluster_id"] != cluster
            or not start <= now < cutoff - timedelta(minutes=10)):
        raise ValueError("Expected the matching active worker scope with time for the bounded session")
    return cutoff


def utcnow():
    return datetime.now(timezone.utc)


class Transaction:
    def __init__(self, client, ca, target, session, intent):
        self.client = client
        self.ca = ca
        self.target = target
        self.session = session
        self.intent = intent

    def call(self, *args, payload=None, client=None, timeout=25):
        return subprocess.run(
            [*(client or self.client), *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, check=True, timeout=timeout,
        ).stdout

    def checkpoint(self, name, value=True):
        write_private(self.session / (name + ".json"), value)

    def checkpoint_value(self, name):
        path = self.session / (name + ".json")
        return json.loads(path.read_bytes()) if path.exists() else None

    def live(self, resource, name):
        value = self.call("-n", "training", "get", resource, name, "--ignore-not-found=true", "-o", "json")
        return json.loads(value) if value.strip() else None

    def owned(self, resource, desired, checkpoint):
        live = self.live(resource, desired["metadata"]["name"])
        if live is None:
            return None
        metadata = live["metadata"]
        expected_uid = self.checkpoint_value(checkpoint + "-uid")
        if (live["kind"] != desired["kind"] or metadata.get("namespace") != "training"
                or metadata.get("labels", {}).get(ATTEMPT) != self.intent["attempt"]
                or (expected_uid is not None and expected_uid != metadata["uid"])
                or any(live.get(key) != value for key, value in desired.items() if key != "metadata")
                or (resource == "secrets" and (live.get("data") or live.get("stringData")))):
            raise ValueError(f"Refusing to reconcile an unexpected {resource} object")
        return str(uuid.UUID(metadata["uid"]))

    def create(self, resource, desired, checkpoint):
        if self.live(resource, desired["metadata"]["name"]) is not None:
            raise ValueError(f"Existing {resource} must be reconciled before activation")
        self.checkpoint(checkpoint + "-started")
        returned = self.call("create", "--validate=strict", "-f", "-", "-o", "json", payload=desired)
        uid = str(uuid.UUID(json.loads(returned)["metadata"]["uid"]))
        self.checkpoint(checkpoint + "-uid", uid)
        if self.owned(resource, desired, checkpoint) != uid:
            raise ValueError(f"Created {resource} object is not visible under its issued UID")
        return uid

    def no_create_audited(self, resource, desired, checkpoint):
        path = self.session / (checkpoint + "-no-create.json")
        if not path.exists():
            return False
        mode = path.stat()
        if path.is_symlink() or mode.st_mode & 0o077 or mode.st_uid != os.getuid():
            raise ValueError("Expected owner-only API audit evidence")
        evidence = json.loads(path.read_bytes())
        if (
            evidence["attempt"] != self.intent["attempt"]
            or evidence["target"] != self.intent["target"]
            or evidence["resource"] != resource
            or evidence["name"] != desired["metadata"]["name"]
            or evidence["stage"] != "ResponseComplete"
            or evidence["verb"] != "create"
            or evidence["status_code"] not in (400, 401, 403, 404, 409, 422)
            or str(uuid.UUID(evidence["audit_id"])) != evidence["audit_id"]
        ):
            raise ValueError("Audit evidence does not prove the attempted create was rejected")
        return True

    def delete_owned(self, resource, desired, checkpoint, group):
        if not self.checkpoint_value(checkpoint + "-started"):
            return
        uid = self.owned(resource, desired, checkpoint)
        if uid is None:
            if self.checkpoint_value(checkpoint + "-uid") is None and not self.no_create_audited(
                    resource, desired, checkpoint):
                raise ValueError(f"Unknown create result for {resource}; retry close or reconcile the API audit")
            return
        if self.checkpoint_value(checkpoint + "-uid") is None:
            self.checkpoint(checkpoint + "-uid", uid)
        name = desired["metadata"]["name"]
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}}
        try:
            self.call("delete", "--raw", f"{group}/{resource}/{name}", "-f", "-", payload=options)
        except (subprocess.SubprocessError, OSError):
            if self.owned(resource, desired, checkpoint) is not None:
                raise
        if self.owned(resource, desired, checkpoint) is not None:
            raise ValueError(f"Deletion of owned {resource} is unconfirmed")

    def policy_list(self):
        return json.loads(self.call("get", POLICY, GUARD, "-o", "json"))

    def wait_policy(self, desired):
        deadline = time.monotonic() + 30
        while True:
            live = self.policy_list()
            policy = next(item for item in live["items"] if item["kind"] == "ValidatingAdmissionPolicy")
            if (policy.get("status", {}).get("observedGeneration") == policy["metadata"]["generation"]
                    or time.monotonic() >= deadline):
                check_active(desired, self.intent["guard"], live)
                return policy
            time.sleep(1)

    def patch_policy(self, operator_policy, desired):
        metadata = operator_policy["metadata"]
        annotations = {key: value for key, value in metadata.get("annotations", {}).items()
                       if key not in (RECEIPT, STAGE, ATTEMPT)}
        annotations.update(desired["metadata"].get("annotations", {}))
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            {"op": "replace", "path": "/spec", "value": desired["spec"]},
            {"op": "add", "path": "/metadata/annotations", "value": annotations},
        ]
        self.call("patch", POLICY, "--type=json", f"--field-manager={self.intent['manager']}",
                  "--patch-file=/dev/stdin", payload=patch)

    def restore_hold(self):
        if not self.checkpoint_value("policy-started"):
            return
        uid = self.checkpoint_value("policy-uid")
        held = self.intent["held"]
        # An activation patch with a lost response may still be in flight. A
        # unique annotation advances the resourceVersion even if the hold is
        # already present, invalidating that stale patch's precondition.
        fence = uuid.uuid4().hex
        fenced = {**held, "metadata": {**held["metadata"], "annotations": {FENCE: fence}}}
        for _ in range(3):
            policy = self.live_policy()
            metadata = policy["metadata"]
            if metadata["uid"] != uid:
                raise ValueError("Refusing to change a replacement admission policy")
            owner = metadata.get("annotations", {}).get(ATTEMPT)
            if owner != self.intent["attempt"] and (
                owner is not None or with_match_defaults(policy["spec"], "matchConstraints") != with_match_defaults(
                    held["spec"], "matchConstraints")
            ):
                raise ValueError("Refusing to change an admission policy owned by another attempt")
            previous_version = metadata["resourceVersion"]
            try:
                self.patch_policy(policy, fenced)
            except (subprocess.SubprocessError, OSError):
                # Reconcile a lost patch response by reading our unique fence.
                pass
            observed = self.live_policy()["metadata"]
            if (observed["uid"] == uid and observed["resourceVersion"] != previous_version
                    and observed.get("annotations", {}).get(FENCE) == fence):
                checked = self.wait_policy(held)["metadata"]
                if checked["uid"] == uid and checked.get("annotations", {}).get(FENCE) == fence:
                    return
                break
        raise ValueError("Admission hold fence is unconfirmed")

    def live_policy(self):
        return next(item for item in self.policy_list()["items"] if item["kind"] == "ValidatingAdmissionPolicy")

    def issue(self, uid, cutoff):
        issued = json.loads(self.call(
            "-n", "training", "create", "token", "episode-submit", "--duration=600s",
            "--bound-object-kind=Secret", f"--bound-object-name={self.intent['marker']['metadata']['name']}",
            f"--bound-object-uid={uid}", "-o", "json",
        ))["status"]
        token = issued["token"]
        if not isinstance(token, str) or not token:
            raise ValueError("The managed API did not return a token")
        url = f"https://{api_host(self.target['cluster_id'], self.target['endpoint'])}:443"
        cluster = {"server": url, "certificate-authority-data": base64.b64encode(self.ca).decode()}
        context = {"cluster": "prod-nebius", "namespace": "training", "user": "episode-submit"}
        config = {
            "apiVersion": "v1", "kind": "Config", "current-context": "prod-nebius",
            "clusters": [{"name": "prod-nebius", "cluster": cluster}],
            "contexts": [{"name": "prod-nebius", "context": context}],
            "users": [{"name": "episode-submit", "user": {"token": token}}],
        }
        write_private(self.session / "kubeconfig", config)
        expiry = timestamp(issued["expirationTimestamp"])
        now = utcnow()
        if not now + timedelta(minutes=1) < expiry <= min(cutoff, now + timedelta(minutes=11)):
            raise ValueError("Unexpected granted native token lifetime")
        scoped = ["kubectl", "--kubeconfig", str(self.session / "kubeconfig"), "--context", "prod-nebius",
                  "--request-timeout=10s"]
        if self.call("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}", client=scoped) != IDENTITY:
            raise ValueError("The managed API returned an unexpected native identity")
        self.checkpoint("issued", {"identity": IDENTITY, "expiry": issued["expirationTimestamp"]})
        return expiry

    def probe(self, images=None):
        args = ["--trainer-image", images["trainer"], "--preflight-image", images["preflight"]] if images else []
        env = dict(os.environ, NEBIUS_SUBMIT_KUBECONFIG=str(self.session / "kubeconfig"))
        subprocess.run(
            [sys.executable, str(ROOT / "workloads/smoke/verify-training-access.py"), *args],
            env=env, capture_output=True, check=True, timeout=80,
        )

    def activate(self, images, cutoff):
        try:
            self.wait_policy(self.intent["held"])
            uid = self.create("secrets", self.intent["marker"], "marker")
            expiry = self.issue(uid, cutoff)
            self.probe()
            live = self.policy_list()
            check_active(self.intent["held"], self.intent["guard"], live)
            held = next(item for item in live["items"] if item["kind"] == "ValidatingAdmissionPolicy")
            self.checkpoint("policy-uid", str(uuid.UUID(held["metadata"]["uid"])))
            self.checkpoint("policy-started")
            self.patch_policy(held, self.intent["active"])
            self.wait_policy(self.intent["active"])
            self.create("roles", self.intent["role"], "role")
            if not utcnow() + timedelta(minutes=1) < expiry <= cutoff:
                raise ValueError("Native token no longer covers activation verification")
            if current_release() != (images, self.intent["receipt"]):
                raise ValueError("The signed release changed during activation")
            check_active(self.intent["active"], self.intent["guard"], self.policy_list())
            self.create("rolebindings", self.intent["binding"], "binding")
            self.probe(images)
            self.checkpoint("activated")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.checkpoint_value("closed-reconciled"):
            return
        issues = []

        def attempt(name, operation):
            try:
                operation()
                return True
            except Exception:
                issues.append(name)
                return False

        attempt("RoleBinding", lambda: self.delete_owned("rolebindings", self.intent["binding"], "binding", RBAC))
        marker_removed = attempt("marker", lambda: self.delete_owned(
            "secrets", self.intent["marker"], "marker", "/api/v1/namespaces/training",
        ))
        attempt("admission hold", self.restore_hold)
        attempt("Role", lambda: self.delete_owned("roles", self.intent["role"], "role", RBAC))
        token_path = self.session / "kubeconfig"
        if marker_removed and token_path.exists():
            def remove_token():
                wait_unauthorized(self.target, self.ca, token_path)
                token_path.unlink()
            attempt("credential invalidation", remove_token)
        if issues:
            raise ValueError("Unconfirmed closeout of " + ", ".join(issues))
        if not self.checkpoint_value("closed"):
            self.checkpoint("closed")
        self.checkpoint("closed-reconciled")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("held-policy", help="Render the checked-in unconditional recovery hold")
    commands.add_parser("check-held", help="Verify the live unconditional hold and Deny binding JSON on stdin")
    activate = commands.add_parser("activate", help="Open and verify one attended native session")
    close = commands.add_parser("close", help="Reconcile and close the original native session")
    for mode in (activate, close):
        mode.add_argument("--operator-config", type=Path, required=True)
        mode.add_argument("--session", type=Path, required=True)
    activate.add_argument("--operator-identity", required=True)
    activate.add_argument("--field-manager", required=True)
    activate.add_argument("--stage", choices=("support", "gpu"), required=True)
    activate.add_argument("--expected-receipt", required=True)
    activate.add_argument("--scope", type=Path, required=True)
    args = parser.parse_args()
    held, guard = held_policy_and_binding()
    if args.command == "held-policy":
        print(json.dumps(held, indent=2))
        return
    if args.command == "check-held":
        check_active(held, guard, json.load(sys.stdin))
        return
    target = {"cluster_id": os.environ["NEBIUS_TRAINING_CLUSTER_ID"],
              "endpoint": os.environ["NEBIUS_API_ENDPOINT"],
              "ca_sha256": os.environ["NEBIUS_CA_BUNDLE_SHA256"]}
    operator_config = args.operator_config.stat()
    if operator_config.st_mode & 0o077 or operator_config.st_uid != os.getuid():
        raise ValueError("Expected an owner-only operator kubeconfig")
    client = ["kubectl", "--kubeconfig", str(args.operator_config), "--context", "prod-nebius", "--request-timeout=15s"]
    ca = pinned_kubeconfig_ca(client, target["cluster_id"], target["ca_sha256"], endpoint=target["endpoint"], direct=True)
    if args.command == "activate":
        if not args.field_manager.strip() or not args.operator_identity.strip():
            raise ValueError("Expected an approved operator identity and policy field manager")
        cutoff = require_window(args.scope, args.stage, target["cluster_id"], utcnow())
        images, receipt = current_release()
        if args.expected_receipt != receipt:
            raise ValueError("Signed deployment changed or differs from the independently approved receipt")
        policy = active_policy(images, receipt, args.stage)
        attempt_id = uuid.uuid4().hex
        policy["metadata"]["annotations"][ATTEMPT] = attempt_id
        role, binding = access_objects(receipt)["items"]
        for obj in (role, binding):
            obj["metadata"]["labels"] = {ATTEMPT: attempt_id}
        marker = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "immutable": True,
                  "metadata": {"name": f"episode-native-{attempt_id}", "namespace": "training",
                               "labels": {ATTEMPT: attempt_id}}}
        intent = {"attempt": attempt_id, "target": target, "operator": args.operator_identity,
                  "manager": args.field_manager, "receipt": receipt, "marker": marker,
                  "role": role, "binding": binding, "guard": guard, "held": held, "active": policy}
        args.session.mkdir(mode=0o700)
        write_private(args.session / "intent.json", intent)
        parent_fd = os.open(args.session.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    session_info = args.session.stat()
    if args.session.is_symlink() or session_info.st_mode & 0o077 or session_info.st_uid != os.getuid():
        raise ValueError("Use the original owner-only session directory")
    with (args.session / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        intent = json.loads((args.session / "intent.json").read_bytes())
        transaction = Transaction(client, ca, target, args.session, intent)
        operator = transaction.call("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}")
        if (intent["target"] != target or not operator or operator != intent["operator"]
                or operator.startswith("system:")):
            raise ValueError("Session belongs to a different operator or managed target")
        if args.command == "activate":
            transaction.activate(images, cutoff)
            print(args.session / "kubeconfig")
        else:
            transaction.close()
            print("Closed native activation session")


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        main()
    except (KeyboardInterrupt, OSError, KeyError, ValueError, TypeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.SubprocessError):
            sys.exit("Native activation command failed; use the session directory to reconcile.")
        sys.exit(f"Native activation refused: {error}")
