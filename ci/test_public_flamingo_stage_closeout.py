"""Offline API snapshots for successful public stage capture and detached closeout."""
from copy import deepcopy
import json
from pathlib import Path
import runpy
import sys
import unittest
from uuid import uuid4

SKY = Path(__file__).resolve().parents[1] / "workloads/sky"
sys.path.insert(0, str(SKY))
CLOSE = runpy.run_path(str(SKY / "public-flamingo-stage-closeout.py"))
RENDER = runpy.run_path(str(SKY / "render-public-flamingo-stage.py"))


class StageCloseout(unittest.TestCase):
    def setUp(self):
        self.ids = {key: str(uuid4()) for key in ("job", "configmap", "inventory")}
        self.pod_uid = str(uuid4())
        self.image = "ghcr.io/example/test@sha256:" + "a" * 64
        self.job_name, self.run = "stage-test", "test"
        self.node = RENDER["CPU_NODE"]
        expected = RENDER["stage_job"](self.run, "stage-test-inventory", "stage-test-program", self.image, "w", "o")
        self.packet = {"run": self.run, "cluster_id": "cluster", "binding": {"provider_disk_id": "disk"},
                       "placement": {"name": self.node, "uid": "cpu-uid", "provider_id": "nebius://" + self.node},
                       "job": expected, "configmap": {"metadata": {"name": "stage-test-program"}, "data": {"objects": "[]"}},
                       "image": self.image, "weights_manifest_sha256": RENDER["WEIGHTS"]}
        self.job = deepcopy(expected)
        self.job["metadata"]["uid"] = self.ids["job"]
        self.job["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
        gen_labels = {"job-name": self.job_name, "batch.kubernetes.io/job-name": self.job_name,
                      "controller-uid": self.ids["job"], "batch.kubernetes.io/controller-uid": self.ids["job"]}
        template = self.job["spec"]["template"]
        template["metadata"]["labels"].update(gen_labels)
        template["spec"]["serviceAccount"] = template["spec"]["serviceAccountName"]
        self.job["spec"].update({"selector": {"matchLabels": {"batch.kubernetes.io/controller-uid": self.ids["job"]}},
                                 "suspend": False, "manualSelector": False, "completionMode": "NonIndexed"})
        self.pod = {"metadata": {"name": "stage-test-abc", "namespace": "training", "uid": self.pod_uid,
                                 "labels": deepcopy(template["metadata"]["labels"]), "ownerReferences": [
                                     {"apiVersion": "batch/v1", "kind": "Job", "controller": True,
                                      "name": self.job_name, "uid": self.ids["job"]}]},
                    "spec": {**deepcopy(template["spec"]), "priority": 0, "preemptionPolicy": "PreemptLowerPriority",
                             "nodeName": self.node, "tolerations": [
                                 {"key": "node.kubernetes.io/not-ready", "operator": "Exists",
                                  "effect": "NoExecute", "tolerationSeconds": 300}]},
                    "status": {"phase": "Succeeded", "containerStatuses": [
                        {"name": "stage", "imageID": self.image, "restartCount": 0, "state": {
                            "terminated": {"exitCode": 0, "finishedAt": "2026-11-22T00:00:00Z"}}}]}}
        self.cm = {"metadata": {"name": "stage-test-program", "namespace": "training", "uid": self.ids["configmap"]},
                   "data": self.packet["configmap"]["data"], "immutable": True}
        self.secret = {"metadata": {"name": "stage-test-inventory", "namespace": "training", "uid": self.ids["inventory"]},
                       "immutable": True, "type": "Opaque", "keys": ["inventory.json"]}
        self.worker = CLOSE["worker_facts"](self.packet)
        self.objects = {"job": self.job, "pod": self.pod, "configmap": self.cm, "pods": {"items": [self.pod]},
                        "jobs": {"items": [self.job]}, "node": {
                            "metadata": {"name": self.node, "uid": "cpu-uid", "labels": {"airaware.training/pool": "training-cpu"}},
                            "spec": {"providerID": self.packet["placement"]["provider_id"]},
                            "status": {"conditions": [{"type": "Ready", "status": "True"}]}}}

    def get(self, kind, *args):
        return self.objects.get(kind, {"items": []})

    def capture(self):
        return CLOSE["capture"](self.packet, self.ids, self.get, lambda *args: json.dumps(self.worker), lambda n: self.secret)

    def fence(self, receipt, absent=lambda *args: True, detached=lambda: True):
        return CLOSE["fence"](self.packet, self.ids, receipt, self.get, absent, detached)

    def test_verified_capture_then_retirement(self):
        receipt = self.capture()
        self.assertEqual(receipt["uids"]["pod"], self.pod_uid)
        self.assertNotIn("writer_fenced", receipt)
        self.objects.update(pods={"items": []}, jobs={"items": []})
        self.assertEqual(self.fence(receipt), {"run": self.run, "cluster_id": "cluster", "disk_id": "disk",
                         "output_subpath": "flamingo-baselines/test", "writer_fenced": True, **self.worker})

    def test_only_exact_suspended_enrollment_can_be_activated(self):
        with self.assertRaises(ValueError):
            CLOSE["verify_job"](self.packet, self.job, suspended=True)
        self.job["spec"]["suspend"] = True
        CLOSE["verify_job"](self.packet, self.job, suspended=True)
        self.job["spec"]["template"]["spec"]["securityContext"]["fsGroup"] = 1000
        with self.assertRaises(ValueError):
            CLOSE["verify_job"](self.packet, self.job, suspended=True)

    def test_rejects_pod_mutations_and_restarts(self):
        self.pod["spec"]["volumes"].append({"name": "root", "hostPath": {"path": "/"}})
        with self.assertRaisesRegex(ValueError, "Executed Pod"):
            self.capture()
        self.pod["spec"]["volumes"].pop()
        self.pod["status"]["containerStatuses"][0]["restartCount"] = 1
        with self.assertRaisesRegex(ValueError, "termination"):
            self.capture()

    def test_bound_region_and_signed_platform_image_are_valid_defaults(self):
        self.pod["metadata"]["labels"]["topology.kubernetes.io/region"] = "eu-south1"
        self.job["spec"]["template"]["metadata"]["creationTimestamp"] = None
        self.pod["status"]["containerStatuses"][0]["imageID"] = CLOSE["PLATFORM_IMAGE"]
        self.assertEqual(self.capture()["image_id"], CLOSE["PLATFORM_IMAGE"])
        self.pod["metadata"]["labels"]["app"] = "another-egress-policy"
        with self.assertRaisesRegex(ValueError, "labels or annotations"):
            self.capture()

    def test_rejects_job_mutation_and_program_replacement(self):
        self.job["spec"]["backoffLimit"] = 1
        with self.assertRaisesRegex(ValueError, "Admitted Job"):
            self.capture()
        self.job["spec"]["backoffLimit"] = 0
        self.cm["data"] = {"objects": "substituted"}
        with self.assertRaisesRegex(ValueError, "program differs"):
            self.capture()

    def test_rejects_substituted_secret_node_or_image(self):
        self.secret["metadata"]["uid"] = str(uuid4())
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.capture()
        self.secret["metadata"]["uid"] = self.ids["inventory"]
        self.objects["node"]["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(ValueError, "placement"):
            self.capture()
        self.objects["node"]["metadata"]["uid"] = "cpu-uid"
        self.pod["status"]["containerStatuses"][0]["imageID"] = "another"
        with self.assertRaisesRegex(ValueError, "executed image"):
            self.capture()

    def test_rejects_incorrect_worker_values_and_extra_claimant(self):
        self.worker["output_empty"] = 1
        with self.assertRaisesRegex(ValueError, "stage output"):
            self.capture()
        self.worker["output_empty"] = True
        self.objects["pods"]["items"].append({"metadata": {}, "spec": self.pod["spec"], "status": {"phase": "Pending"}})
        with self.assertRaisesRegex(ValueError, "Another Pod"):
            self.capture()

    def test_rejects_pending_retirement_and_mutated_evidence(self):
        receipt = self.capture()
        for kind in ("job", "pod", "secret", "configmap"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "retirement"):
                self.fence(receipt, lambda k, n: k != kind)
        with self.assertRaisesRegex(ValueError, "retired Job remains"):
            self.fence(receipt)
        self.objects["pods"]["items"] = []
        with self.assertRaisesRegex(ValueError, "attachment"):
            self.fence(receipt, detached=lambda: False)
        receipt["packet_sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "approved packet"):
            self.fence(receipt)

    def test_retry_capable_and_scheduled_workloads_prevent_fence(self):
        receipt = self.capture()
        self.objects["pods"]["items"] = []
        other = deepcopy(self.job)
        other["metadata"]["uid"] = str(uuid4())
        other["status"] = {"active": 0}
        self.objects["jobs"]["items"] = [other]
        with self.assertRaisesRegex(ValueError, "Another Job"):
            self.fence(receipt)
        self.objects["jobs"]["items"] = []
        self.objects["cronjobs"] = {"items": [{"spec": {"suspend": True, "jobTemplate": {"spec": other["spec"]}}}]}
        with self.assertRaisesRegex(ValueError, "controller"):
            self.fence(receipt)


if __name__ == "__main__":
    unittest.main()
