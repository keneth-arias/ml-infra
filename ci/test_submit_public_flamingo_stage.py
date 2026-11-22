import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SKY = Path(__file__).resolve().parents[1] / "workloads" / "sky"
sys.path.insert(0, str(SKY))
spec = importlib.util.spec_from_file_location("submit_public", SKY / "submit-public-flamingo-stage.py")
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)


class PublicStageSubmission(unittest.TestCase):
    def test_packet_requires_approved_bytes_and_reproduced_render(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_bytes(b'{"run":"example"}')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for pin, reproduced in [(digest, {"run": "different"}), ("0" * 64, {"run": "example"})]:
                with self.subTest(pin=pin), self.assertRaises(ValueError):
                    submit.approved_packet(path, pin, reproduced)
            self.assertEqual(submit.approved_packet(path, digest, {"run": "example"}), {"run": "example"})

    def test_activation_requires_successor_of_original_exclusive_handoff(self):
        guard = {"policy_uid": "policy", "binding_uid": "binding", "binding_generation": 1,
                 "policy_generation": 4}
        enrollment = dict(action="enroll", purpose="public-flamingo-stage", run="r",
                          packet_file_sha256="pin", deployment_sha256="deployment", inventory_secret="secret",
                          inventory_uid="inventory", staging_operator="stage", storage_operator="storage",
                          exclusive_storage_handoff=True, inventory_secret_immutable=True,
                          expires_at="2026-11-22T01:10:00Z", closed_guard=guard)
        active = dict(enrollment, action="activate", stage_guard=dict(guard, policy_generation=5, job_uid="job", expires_at=enrollment["expires_at"]))
        submit.check_transition(enrollment, active)
        for override in ({"storage_operator": "other"}, {"expires_at": "2026-11-22T02:10:00Z"},
                         {"stage_guard": dict(active["stage_guard"], policy_generation=6)}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                submit.check_transition(enrollment, dict(active, **override))

    def test_placement_rejects_replacement_and_unschedulable_node(self):
        placement = {"name": "cpu-node", "provider_id": "nebius://cpu-node", "uid": "original"}
        node = {"metadata": {"name": "cpu-node", "uid": "original", "labels": {"airaware.training/pool": "training-cpu"}},
                "spec": {"providerID": placement["provider_id"]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"cpu": "4"}}}
        submit.check_placement(placement, node)
        node["spec"]["unschedulable"] = True
        with self.assertRaises(ValueError):
            submit.check_placement(placement, node)
        node["spec"].pop("unschedulable")
        node["metadata"]["uid"] = "replacement"
        with self.assertRaises(ValueError):
            submit.check_placement(placement, node)

    def test_create_returns_uid_and_never_discloses_admission_diagnostics(self):
        doc = {"kind": "Job", "metadata": {"name": "stage", "namespace": "training"}}
        with patch.object(submit.subprocess, "run") as call:
            call.return_value.returncode = 1
            call.return_value.stderr = "private diagnostic"
            with self.assertRaisesRegex(RuntimeError, "inspect") as error:
                submit.create(["kubectl"], doc)
            self.assertNotIn("private diagnostic", str(error.exception))
            call.return_value.returncode = 0
            call.return_value.stdout = json.dumps({**doc, "metadata": {**doc["metadata"], "uid": "issued"}})
            self.assertEqual(submit.create(["kubectl"], doc), "issued")
