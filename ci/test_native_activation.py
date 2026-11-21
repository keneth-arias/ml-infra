from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bootstrap/nebius/management/native-activation.py"
RECEIPT = "a" * 64


class NativeActivationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = runpy.run_path(str(TOOL))
        cls.policy, cls.binding = cls.functions["held_policy_and_binding"]()
        cls.images = {
            role: next(v["expression"][1:-1] for v in cls.policy["spec"]["variables"]
                       if v["name"] == role + "Image")
            for role in ("trainer", "preflight")
        }

    def test_held_default_and_only_bounded_policy_changes(self):
        make = self.functions["active_policy"]
        hold = self.policy["spec"]["validations"]
        self.assertEqual(hold[0]["expression"], "false")
        support = make(self.images, RECEIPT, "support")
        gpu = make(self.images, RECEIPT, "gpu")
        self.assertEqual(gpu["spec"]["validations"], hold[1:])
        self.assertEqual(support["spec"]["validations"][1:], hold[1:])
        self.assertIn("request.dryRun", support["spec"]["validations"][0]["expression"])
        self.assertEqual(support["spec"]["variables"], self.policy["spec"]["variables"])
        with self.assertRaises(ValueError):
            make(self.images | {"trainer": self.images["preflight"]}, RECEIPT, "gpu")

    def test_live_policy_binding_generation_and_warnings_are_gated(self):
        make = self.functions["active_policy"]
        check = self.functions["check_active"]
        expected = make(self.images, RECEIPT, "support")
        live = deepcopy(expected)
        live["metadata"]["generation"] = 7
        live["status"] = {"observedGeneration": 7, "typeChecking": {}}
        check(expected, self.binding, {"items": [live, self.binding]})
        for edit in (
            lambda p, b: p["spec"]["validations"].pop(),
            lambda p, b: p["metadata"]["annotations"].clear(),
            lambda p, b: p["status"].update(observedGeneration=6),
            lambda p, b: p["status"]["typeChecking"].update(expressionWarnings=[{"warning": "bad"}]),
            lambda p, b: b["spec"].update(validationActions=["Warn"]),
        ):
            p, b = deepcopy(live), deepcopy(self.binding)
            edit(p, b)
            with self.assertRaises(ValueError):
                check(expected, self.binding, {"items": [p, b]})

    def test_access_exact_scope(self):
        role, binding = self.functions["access_objects"](RECEIPT)["items"]
        self.assertEqual(role["metadata"]["namespace"], "training")
        self.assertEqual(role["rules"], [
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get", "list", "watch"]},
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        ])
        self.assertEqual(binding["subjects"], [
            {"kind": "ServiceAccount", "name": "episode-submit", "namespace": "training"}
        ])

    def test_window_scope_rejects_wrong_target_and_elapsed_cutoff(self):
        check = self.functions["require_window"]
        scope = {"stage": "support", "cluster_id": "mk8scluster-fixture",
                 "start": "2026-01-01T00:00:00Z", "cutoff": "2026-01-01T01:00:00Z"}
        now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "scope.json"
            path.write_text(json.dumps(scope))
            path.chmod(0o600)
            check(path, "support", scope["cluster_id"], now)
            for stage, cluster, observed in (
                ("gpu", scope["cluster_id"], now),
                ("support", "mk8scluster-other", now),
                ("support", scope["cluster_id"], now.replace(minute=50)),
            ):
                with self.assertRaises(ValueError):
                    check(path, stage, cluster, observed)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                check(path, "support", scope["cluster_id"], now)

    def test_failed_activation_reconciles_owned_resources(self):
        from datetime import timedelta
        import subprocess
        from unittest.mock import patch
        import uuid

        functions = self.functions
        transaction = functions["Transaction"]
        attempt_tag = functions["ATTEMPT"]
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(minutes=9)).isoformat().replace("+00:00", "Z")
        too_late = (now + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        cutoff = now + timedelta(hours=1)
        target = {"cluster_id": "mk8scluster-fixture", "endpoint": "public"}
        case_names = ("marker-response", "invalid-expiry", "held-probe", "policy-response",
                      "binding-response", "released-probe", "binding-replaced", "success")

        for failure in case_names:
            with self.subTest(failure=failure), TemporaryDirectory() as directory:
                session = Path(directory)
                attempt = uuid.uuid4().hex
                active = functions["active_policy"](self.images, RECEIPT, "support")
                active["metadata"]["annotations"][attempt_tag] = attempt
                role, binding = functions["access_objects"](RECEIPT)["items"]
                for item in (role, binding):
                    item["metadata"]["labels"] = {attempt_tag: attempt}
                marker = {"apiVersion": "v1", "kind": "Secret", "immutable": True, "type": "Opaque",
                          "metadata": {"name": "marker", "namespace": "training", "labels": {attempt_tag: attempt}}}
                intent = {"attempt": attempt, "held": self.policy, "active": active, "guard": self.binding,
                          "marker": marker, "role": role, "binding": binding, "receipt": RECEIPT}
                objects, calls = {}, []
                policy_uid = str(uuid.uuid4())
                kind_keys = {"Secret": "secrets", "Role": "roles", "RoleBinding": "rolebindings"}

                class FakeTransaction(transaction):
                    def __init__(self):
                        super().__init__(["operator"], b"ca", target, session, intent)
                        self.policy = intent["held"]

                    def call(self, *args, payload=None, **kwargs):
                        if "get" in args:
                            return json.dumps(objects[args[3]]) if args[3] in objects else ""
                        if args[:1] == ("delete",):
                            resource = args[2].split("/")[-2]
                            self.assert_uid(resource, payload)
                            calls.append("delete-" + resource)
                            del objects[resource]
                            return "{}"
                        if "token" in args and "create" in args:
                            calls.append("token")
                            return json.dumps({"status": {"token": "synthetic", "expirationTimestamp":
                                                          too_late if failure == "invalid-expiry" else expiry}})
                        if args[:2] == ("auth", "whoami"):
                            calls.append("token-identity")
                            return functions["IDENTITY"]
                        if args[0] == "create":
                            resource = kind_keys[payload["kind"]]
                            saved = deepcopy(payload)
                            saved["metadata"]["uid"] = str(uuid.uuid4())
                            objects[resource] = saved
                            calls.append("create-" + resource)
                            if (failure, resource) in (
                                ("marker-response", "secrets"), ("binding-response", "rolebindings")
                            ):
                                raise subprocess.TimeoutExpired(args, 1)
                            return json.dumps(saved)
                        raise AssertionError(args)

                    def assert_uid(self, resource, delete):
                        if delete["preconditions"]["uid"] != objects[resource]["metadata"]["uid"]:
                            raise AssertionError("UID precondition differs from live object")

                    def policy_list(self):
                        live = deepcopy(self.policy)
                        live["metadata"]["generation"] = 7
                        live["metadata"]["uid"] = policy_uid
                        live["status"] = {"observedGeneration": 7, "typeChecking": {}}
                        return {"items": [live, intent["guard"]]}

                    def patch_policy(self, live, desired):
                        self.policy = desired
                        if desired is active and failure == "policy-response":
                            raise subprocess.TimeoutExpired("patch", 1)

                    def probe(self, images=None):
                        mode = "released-probe" if images else "held-probe"
                        calls.append(mode)
                        if failure == "binding-replaced" and images:
                            objects["rolebindings"]["metadata"]["uid"] = str(uuid.uuid4())
                            raise ValueError("synthetic replacement during released probe")
                        if failure == mode:
                            raise ValueError("synthetic probe failure")

                tx = FakeTransaction()
                globals_ = transaction.activate.__globals__
                with patch.dict(globals_, current_release=lambda: (self.images, RECEIPT),
                                wait_unauthorized=lambda *args: calls.append("401")):
                    if failure == "success":
                        tx.activate(self.images, cutoff)
                        self.assertTrue((session / "kubeconfig").exists())
                        tx.close()
                        tx.close()
                    else:
                        with self.assertRaises((ValueError, subprocess.TimeoutExpired)):
                            tx.activate(self.images, cutoff)

                if failure == "binding-replaced":
                    self.assertEqual(set(objects), {"rolebindings"})
                    self.assertNotIn("delete-rolebindings", calls)
                    self.assertFalse((session / "closed.json").exists())
                else:
                    self.assertEqual(objects, {})
                    self.assertTrue((session / "closed.json").is_file())
                self.assertEqual(tx.policy, self.policy)
                self.assertFalse((session / "kubeconfig").exists())
                if "create-rolebindings" in calls:
                    self.assertLess(calls.index("token-identity"), calls.index("create-rolebindings"))
                    self.assertLess(calls.index("held-probe"), calls.index("create-rolebindings"))
                    if failure != "binding-replaced":
                        self.assertLess(calls.index("delete-rolebindings"), calls.index("delete-secrets"))
                if failure not in ("marker-response",):
                    self.assertIn("401", calls)
