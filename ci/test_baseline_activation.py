"""CPU tests for the one-Job Flamingo window."""
import copy
import json
from pathlib import Path
import runpy
import unittest
import uuid
from datetime import timedelta
from io import BytesIO
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import os

from ci.test_gpu_plan import scope as window, ledger, NOW as CLOCK, utc

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bootstrap/nebius/management/flamingo-baseline.py"
B = runpy.run_path(str(TOOL))
UID = str(uuid.uuid4())
JOB = B["render"]("ghcr.io/keneth-arias/ml-infra-qwen@sha256:" + "a"*64,
                  "probe", "computeinstance-a", 300, 100)



class GuardTest(unittest.TestCase):
    def test_enrollment_checks_defaulted_job_and_rejects_mutations(self):
        job = copy.deepcopy(JOB)
        job["spec"]["suspend"] = True
        observed = json.loads(json.dumps(job))
        observed["metadata"].update(uid=UID, generation=1, resourceVersion="45")
        labels = {"batch.kubernetes.io/controller-uid": UID, "controller-uid": UID,
                  "job-name": job["metadata"]["name"], "batch.kubernetes.io/job-name": job["metadata"]["name"]}
        observed["spec"]["template"]["metadata"]["labels"].update(labels)
        observed["spec"]["selector"] = {"matchLabels": {"batch.kubernetes.io/controller-uid": UID}}
        observed["spec"]["template"]["spec"]["dnsPolicy"] = "ClusterFirst"
        self.assertEqual(B["verified_enrollment"](job, observed), UID)
        pod = observed["spec"]["template"]["spec"]
        for target, key, value in [(pod, "initContainers", []), (pod, "nodeName", "wrong"),
                                   (observed["spec"], "suspend", False), (pod["containers"][0], "args", ["x"])]:
            prev = copy.deepcopy(observed)
            target[key] = value
            with self.assertRaises(ValueError):
                B["verified_enrollment"](job, observed)
            target.pop(key)
            if key == "suspend":
                target[key] = prev["spec"][key]

    def test_only_named_patch_and_equal_template(self):
        role = B["baseline_role"]("a"*64, JOB["metadata"]["name"])
        self.assertEqual(role["rules"], [{"apiGroups":["batch"], "resources":["jobs"],
                                         "resourceNames":[JOB["metadata"]["name"]], "verbs":["get", "patch"]}])
        checks = B["baseline_checks"](JOB, UID)
        expressions = ' '.join(c["expression"] for c in checks)
        for fragment in (UID, "oldObject.spec.suspend", "!object.spec.suspend", "object.spec.template == oldObject.spec.template",
                         "request.operation == 'UPDATE'", "object.spec.activeDeadlineSeconds"):
            self.assertIn(fragment, expressions)

    def test_exact_signed_renderer_staging_and_binding(self):
        scope = dict(window(), stage="gpu", project_id="project-1")
        info = {"phase": "all", "cluster_id": scope["cluster_id"], "project_id": "project-1",
                "bindings": [{"namespace": "training", "claim": "airaware-checkpoints",
                              "pvc_uid": "pvc", "pv_uid": "pv", "csi_handle": "computedisk-1",
                              "size_gib": 128, "provider_disk_id": "computedisk-1"}]}
        stage = {"cluster_id": scope["cluster_id"], "run": "probe", "disk_id": "computedisk-1",
                 "weights_manifest_sha256": B["R"]["FLAMINGO_WEIGHTS"], "output_subpath": "flamingo-baselines/probe",
                 "output_uid": 1000, "output_empty": True, "writer_fenced": True}
        raw = B["canonical"](JOB)
        review = {"schema": "flamingo-baseline-review-v1", "provider_id": "nebius://computeinstance-a",
                  "cluster_id": scope["cluster_id"], "deployment_sha256": "f"*64,
                  "job_sha256": B["sha"](raw), "job_json": raw.decode()}
        with TemporaryDirectory() as folder, patch.dict(os.environ, NEBIUS_TRAINING_CLUSTER_ID=scope["cluster_id"]):
            args = SimpleNamespace(node_uid=UID, expected_deployment="f"*64)
            def save(key, obj):
                path = Path(folder) / key
                path.write_text(json.dumps(obj))
                path.chmod(0o600)
                setattr(args, key, path)
            for key, obj in (("scope", scope), ("binding", info), ("staging", stage), ("review", review)):
                save(key, obj)
            def describe():
                return B["baseline_request"](args, scope, "c"*64,
                                             {"trainer": JOB["spec"]["template"]["spec"]["containers"][0]["image"]}, "f"*64)
            request, job, binding = describe()
            self.assertEqual((job, binding[2], request["node_uid"]), (JOB, "computedisk-1", UID))
            self.assertEqual(request["staging_sha256"], B["sha"](B["canonical"](stage)))
            for key, obj in (("review", dict(review, job_json=raw.decode().replace("/output", "/opt"))),
                             ("review", dict(review, deployment_sha256="0"*64)),
                             ("staging", dict(stage, writer_fenced=False)),
                             ("staging", dict(stage, weights_manifest_sha256="0"*64))):
                save(key, obj)
                with self.assertRaises(ValueError):
                    describe()
                save(key, review if key == "review" else stage)


class VersionedLedger:
    def __init__(self, data):
        self.raw = json.dumps(data).encode()
        self.version = 13

    def get_object(self, *, Bucket, Key):
        assert Bucket == "controls" and Key == B["LEDGER_KEY"]
        return {"Body": BytesIO(self.raw), "VersionId": str(self.version), "ETag": B["sha"](self.raw)}

    def put_object(self, *, Bucket, Key, Body, IfMatch, **kwargs):
        assert Bucket == "controls" and Key == B["LEDGER_KEY"] and IfMatch == B["sha"](self.raw)
        self.version += 1
        self.raw = Body
        return {"VersionId": str(self.version)}


class FundingTest(unittest.TestCase):
    def setUp(self):
        self.scope = dict(window(), stage="gpu")
        self.request = {k: self.scope[k] for k in ("cluster_id", "cutoff", "start")}
        self.request.update(vm="computeinstance-a", attempt_id="flamingo-base-probe", deadline_seconds=300,
                            termination_grace_seconds=30)
        self.funds = dict(ledger(), gpu_intervals=[dict(vm="computeinstance-a", start=self.scope["start"], end=None)],
                          baseline_approval=self.request, active_reservation=None)

    def test_reserve_recheck_publish_once(self):
        store = VersionedLedger(self.funds)
        read = B["reserve"](store, self.scope, B["sha"](store.raw), "13", self.request, CLOCK)
        self.assertEqual(read["version_id"], "14")
        B["check_reserved"](store, self.scope, read, CLOCK)
        with self.assertRaises(ValueError):
            B["reserve"](store, self.scope, B["sha"](store.raw), "14", self.request, CLOCK)
        submission = {"funding": read, "job": "flamingo-base-probe", "namespace": "training", "uid": UID}
        first = B["record_submission"](store, self.scope, submission, CLOCK)
        self.assertEqual(first, B["record_submission"](store, self.scope, submission, CLOCK))
        self.assertEqual((first["uid"], first["version_id"], first["funding_version"]), (UID, "15", "14"))
        with self.assertRaises(ValueError):
            B["record_submission"](store, self.scope, dict(submission, uid=str(uuid.uuid4())), CLOCK)
        self.assertEqual(store.version, 15)
        self.assertEqual(json.loads(store.raw)["active_reservation"], read["hold"])

    def test_short_job_requires_funding_through_vm_cutoff(self):
        # With ten hours to cutoff, either cap can leave just fifteen minutes.
        prior_vm = dict(vm="previous", start=utc(CLOCK - timedelta(hours=98, minutes=45)),
                        end=self.scope["start"])
        for changes in ({"accrued_gpu_usd": "148.125"},
                        {"gpu_intervals": [prior_vm, *self.funds["gpu_intervals"]]}):
            funds = dict(self.funds, **changes)
            store = VersionedLedger(funds)
            with self.subTest(changes=changes, phase="reserve"), self.assertRaisesRegex(ValueError, "Full remaining"):
                B["reserve"](store, self.scope, B["sha"](store.raw), "13", self.request, CLOCK)
            self.assertEqual(store.version, 13)

            # Dispatch must also reject a hold issued before the full-window check.
            hold = {"request": self.request}
            store = VersionedLedger(dict(funds, active_reservation=hold))
            receipt = dict(version_id="13", revision=funds["revision"], ledger_sha256=B["sha"](store.raw), hold=hold)
            with self.subTest(changes=changes, phase="dispatch"), self.assertRaisesRegex(ValueError, "Full remaining"):
                B["check_reserved"](store, self.scope, receipt, CLOCK)

    def test_exactly_funded_vm_window_allows_reservation_and_dispatch(self):
        prior_vm = dict(vm="previous", start=utc(CLOCK - timedelta(hours=89)), end=self.scope["start"])
        store = VersionedLedger(dict(self.funds, gpu_intervals=[prior_vm, *self.funds["gpu_intervals"]],
                                     accrued_gpu_usd="133.50"))
        receipt = B["reserve"](store, self.scope, B["sha"](store.raw), "13", self.request, CLOCK)
        B["check_reserved"](store, self.scope, receipt, CLOCK + timedelta(minutes=6))

    def test_exhaustion_stale_approval_wrong_vm_and_replay_fail_before_cas(self):
        for changes, req, version, clock in (
            ({"active_reservation": {"stage": "fit"}}, self.request, "13", CLOCK),
            ({"attempt_ids": [self.request["attempt_id"]]}, self.request, "13", CLOCK),
            ({}, self.request, "12", CLOCK),
            ({}, dict(self.request, deadline_seconds=301), "13", CLOCK),
            ({"gpu_intervals": [dict(vm="other", start=self.scope["start"], end=None)]}, self.request, "13", CLOCK),
            ({"accrued_gpu_usd": "250"}, self.request, "13", CLOCK),
            ({}, self.request, "13", CLOCK + timedelta(minutes=16)),
            ({"drain_hours": "9.9"}, self.request, "13", CLOCK),
        ):
            store = VersionedLedger(dict(self.funds, **changes))
            with self.subTest(changes=changes, version=version), self.assertRaises(ValueError):
                B["reserve"](store, self.scope, B["sha"](store.raw), version, req, clock)
            self.assertEqual(store.version, 13)

    def test_stopped_vm_refuses_even_while_kubernetes_reports_ready(self):
        place = B["verify_placement"]
        request = dict(self.request, node_uid=UID, node="gpu", provider_id="nebius://computeinstance-a")
        node = {"metadata": {"uid": UID, "name": "gpu", "labels": {"airaware.training/pool": "training-gpu"}},
                "spec": {"providerID": request["provider_id"]}, "status": {"allocatable": {"nvidia.com/gpu": "1"},
                                                                         "conditions": [{"type":"Ready", "status":"True"}]}}
        vm = {"metadata": {"id": self.request["vm"], "parent_id": "project"}, "status": {"state": "STOPPED"}}
        replies = iter((node, vm))
        with patch.dict(place.__globals__, output=lambda *args: json.dumps(next(replies))):
            with self.assertRaisesRegex(ValueError, "provider VM"):
                place(["kubectl"], request, ("pvc", "pv", "disk"), "project")
