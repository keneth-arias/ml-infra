from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workloads" / "sky"))
import public_flamingo_stage_guard as guard


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.run = "public-gemma"
        self.job = {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "stage-public-gemma",
                    "namespace": "training"}, "spec": {"template": {"spec": {
                        "serviceAccountName": "episode-input-stager", "containers": [{"name": "stage"}]}}}}
        self.objects = {}
        self.pins = {}

    def observe(self, phase):
        if phase != "closed":
            self.pins["job_uid"] = str(uuid.uuid4())
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

    def test_prevent_ambiguous_enrollment_and_render_closed_cli(self):
        with self.assertRaises(ValueError):
            guard.render_guard(self.run, "owner", {"job_uid": str(uuid.uuid4())}, self.job)
        proc = subprocess.run([sys.executable, guard.__file__, "--run", self.run, "--phase", "closed"],
                              capture_output=True, check=True)
        items = json.loads(proc.stdout)["items"]
        self.assertEqual([p["kind"] for p in items], ["ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"])


if __name__ == "__main__":
    unittest.main()
