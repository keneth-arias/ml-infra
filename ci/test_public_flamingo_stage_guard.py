from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workloads" / "sky"))
import public_flamingo_stage_guard as guard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.run = "public-gemma"
        self.job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "stage-public-gemma",
                    "namespace": "training"}, "spec": {"activeDeadlineSeconds": 1800, "template": {"spec": {
                        "serviceAccountName": "episode-input-stager", "containers": [{"name": "stage"}]}}}}
        self.objects = {}
        self.pins = {}

    def observe(self, phase):
        if phase != "closed":
            self.pins["job_uid"] = str(uuid.uuid4())
            self.pins["expires_at"] = "2026-11-22T02:00:00Z"
            self.objects["job"] = deepcopy(self.job)
            self.objects["job"]["metadata"]["uid"] = self.pins["job_uid"]
        policy, binding = guard.render_guard(self.run, phase, self.pins, self.job)
        for kind, obj in (("policy", policy), ("binding", binding)):
            uid = str(uuid.uuid4())
            self.pins.update({kind + "_uid": uid, kind + "_generation": 1})
            obj["metadata"].update(uid=uid, generation=1)
            if kind == "policy":
                obj["status"] = {"observedGeneration": 1, "typeChecking": {}}
            self.objects[obj["kind"].lower()] = obj
        return policy, binding

    def get(self, kind, name, *args):
        return self.objects[kind]

    def test_successor_guard_pins_even_closed_phase_and_reproduces_both_jobs(self):
        sha = "7" * 64
        run = "fsw-" + sha[:32]
        w = SimpleNamespace(run=run, sha256=sha, cpu_node="computeinstance-successor",
                            preflight_image="repo@sha256:" + "b" * 64, objects_sha256="c" * 64,
                            start="2028-01-01T00:00:00Z", cutoff="2028-01-01T04:00:00Z")
        pins = {"window_sha256": sha, "expires_at": "2028-01-01T05:00:00Z", "job_uid": str(uuid.uuid4())}
        closed, _ = guard.render_guard(run, "closed", {"window_sha256": sha}, window=w)
        self.assertEqual(closed["metadata"]["name"], guard.NAME)
        self.assertIn(sha, str(closed["spec"]["auditAnnotations"]))
        for wrong in ({}, {"window_sha256": "d"*64}):
            with self.assertRaises(ValueError):
                guard.render_guard(run, "closed", wrong, window=w)
        for phase in ("stage", "owner"):
            job = guard.reviewed_window_job(w, run, phase)
            policy, _ = guard.render_guard(run, phase, pins, job, w)
            self.assertIn(w.start, str(policy["spec"]["validations"]))
            self.assertIn("2028-01-01T03:", str(policy["spec"]["validations"]))
            mutations = [
                lambda j: j["spec"]["template"]["spec"].update(affinity={}),
                lambda j: j["spec"]["template"]["spec"]["containers"][0].update(image="wrong"),
                lambda j: j["spec"]["template"]["spec"]["containers"][0].update(volumeMounts=[]),
                lambda j: j["spec"]["template"]["spec"]["containers"][0].update(command=["wrong"]),
                lambda j: j["metadata"].pop("annotations"),
            ]
            for mutate in mutations:
                changed = deepcopy(job)
                mutate(changed)
                with self.assertRaisesRegex(ValueError, "reproduced"):
                    guard.render_guard(run, phase, pins, changed, w)

    def test_closed_denies_new_consumers_without_a_job_parameter(self):
        policy, binding = self.observe("closed")
        self.assertNotIn("paramKind", policy["spec"])
        self.assertNotIn("paramRef", binding["spec"])
        rules = policy["spec"]["validations"]
        self.assertTrue(any(row["expression"].startswith("request.operation == 'UPDATE'") for row in rules))
        self.assertEqual(guard.verify_guard(self.get, self.run, "closed", self.pins), self.pins)

    def test_active_pins_job_and_validates_full_admitted_spec(self):
        policy, _ = self.observe("stage")
        rules = [row["expression"] for row in policy["spec"]["validations"]]
        self.assertIn(self.pins["job_uid"], str(rules))
        self.assertIn(guard.CONTROLLER, str(rules))
        self.assertIn("object.metadata.ownerReferences", str(rules))
        self.assertIn("object.metadata.labels", str(rules))
        self.assertIn("object.metadata.annotations", str(rules))
        self.assertTrue(any("dyn(object.spec) == dyn(" + guard.cel(guard.expected_spec(self.job)) + ")" in r
                            for r in rules))
        self.assertEqual(guard.verify_guard(self.get, self.run, "stage", self.pins, self.job), self.pins)

    def test_changed_reviewed_command_or_job_identity_rejected(self):
        self.observe("stage")
        changed = deepcopy(self.job)
        changed["spec"]["template"]["spec"]["containers"][0]["command"] = ["another-command"]
        with self.assertRaises(ValueError):
            guard.verify_guard(self.get, self.run, "stage", self.pins, changed)
        self.objects["job"]["metadata"]["uid"] = str(uuid.uuid4())
        with self.assertRaises(ValueError):
            guard.verify_guard(self.get, self.run, "stage", self.pins, self.job)

    def test_reject_weakened_scope_policy_action_replacement_or_stale_check(self):
        policy, binding = self.observe("stage")
        for obj, key, value in ((policy["spec"], "failurePolicy", "Ignore"),
                                (policy["spec"], "matchConditions", []),
                                (binding["spec"], "validationActions", ["Warn"]),
                                (policy["metadata"], "uid", str(uuid.uuid4())),
                                (policy["status"], "observedGeneration", 0),
                                (policy["status"], "typeChecking", {"expressionWarnings": [{"warning": "unknown"}]})):
            with self.subTest(key=key):
                prev, obj[key] = obj[key], value
                with self.assertRaises(ValueError):
                    guard.verify_guard(self.get, self.run, "stage", self.pins, self.job)
                obj[key] = prev

    def test_all_identities_and_mutating_pod_subresources_in_scope(self):
        primary = self.observe("stage")[0]["spec"]
        match = primary["matchConstraints"]
        self.assertEqual(match["objectSelector"], {})
        rule, = match["resourceRules"]
        self.assertEqual(set(rule["resources"]), {"pods", "pods/ephemeralcontainers", "pods/resize"})
        self.assertEqual(set(rule["operations"]), {"CREATE", "UPDATE"})
        self.assertNotIn("userInfo", str(primary["matchConditions"]))
        self.assertIn("airaware-checkpoints", str(primary["matchConditions"]))
        self.assertIn("oldObject.spec.volumes", str(primary["matchConditions"]))

    def test_latest_server_start_reserves_runtime_and_margin_within_both_expiries(self):
        _policy, _binding = self.observe("stage")
        for expiry, expected in [("2026-11-22T02:01:00Z", "2026-11-22T01:29:00Z"),
                                 ("2099-01-01T00:00:00Z", "2026-11-22T01:53:00Z")]:
            self.pins["expires_at"] = expiry
            policy, _ = guard.render_guard(self.run, "stage", self.pins, self.job)
            rules = [row["expression"] for row in policy["spec"]["validations"]]
            self.assertTrue(any("request.operation != 'CREATE'" in expr
                                and "timestamp(dyn(object.metadata).creationTimestamp)" in expr
                                and expected in expr for expr in rules))
        self.pins["expires_at"] = "2026-11-22T02:01:00"
        with self.assertRaises(ValueError):
            guard.render_guard(self.run, "stage", self.pins, self.job)

    def test_run_inventory_is_matched_without_claim_for_all_pod_secret_paths(self):
        for phase in ("stage", "closed"):
            policy, _ = self.observe(phase)
            condition, = policy["spec"]["matchConditions"]
            expression = condition["expression"]
            self.assertIn("stage-public-gemma-inventory", expression)
            for obj in ("object", "oldObject"):
                for field in ("volumes", "containers", "initContainers", "ephemeralContainers", "imagePullSecrets"):
                    self.assertIn(obj + ".spec." + field, expression)
            for volume in ("secret", "azureFile", "cephfs", "cinder", "flexVolume", "rbd", "scaleIO",
                           "iscsi", "storageos", "csi", "projected"):
                self.assertIn("v." + volume, expression)
            for field in ("valueFrom.secretKeyRef", "secretRef", "nodePublishSecretRef", "envFrom"):
                self.assertIn(field, expression)

    def test_preexisting_inventory_consumer_checks_every_direct_reference(self):
        name = "stage-public-gemma-inventory"
        direct_volumes = [{kind: {"secretRef": {"name": name}}}
                          for kind in ("cephfs", "cinder", "flexVolume", "rbd", "scaleIO", "iscsi", "storageos")]
        direct_volumes.extend([{"secret": {"secretName": name}}, {"azureFile": {"secretName": name}},
                               {"csi": {"nodePublishSecretRef": {"name": name}}},
                               {"projected": {"sources": [{"configMap": {"name": name}}, {"secret": {"name": name}}]}}])
        specs = [{"volumes": [v]} for v in direct_volumes] + [{"imagePullSecrets": [{"name": name}]}]
        for field in ("containers", "initContainers", "ephemeralContainers"):
            specs.extend([{field: [{"env": [{"valueFrom": {"secretKeyRef": {"name": name, "key": "key"}}}]}]},
                          {field: [{"envFrom": [{"secretRef": {"name": name}}]}]}])
        for spec in specs:
            with self.subTest(spec=spec):
                self.assertTrue(guard.preexisting_secret_consumer({"spec": spec}, name))
                self.assertFalse(guard.preexisting_secret_consumer({"spec": spec}, "different-run"))
        self.assertFalse(guard.preexisting_secret_consumer({"spec": {"volumes": [
            {"configMap": {"name": name}}, {"projected": {"sources": [{}]}}]}}, name))

    def test_prevent_ambiguous_enrollment_and_render_closed_cli(self):
        with self.assertRaises(ValueError):
            guard.render_guard(self.run, "owner", {"job_uid": str(uuid.uuid4())}, self.job)
        proc = subprocess.run([sys.executable, guard.__file__, "--run", self.run, "--phase", "closed"],
                              capture_output=True, check=True)
        items = json.loads(proc.stdout)["items"]
        self.assertEqual([p["kind"] for p in items], ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])


if __name__ == "__main__":
    unittest.main()
