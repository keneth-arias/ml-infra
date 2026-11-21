import json
from pathlib import Path
import runpy
import subprocess
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
PILOT = runpy.run_path(str(ROOT / "workloads/smoke/native-cpu-pilot.py"))
UID = "f8f086e7-5dce-41e3-8ec7-18cd9ce77fea"


class NativePilotTest(unittest.TestCase):
    def setUp(self):
        self.name = "cpu-abc"
        self.obj = PILOT["job"]("abc", "worker")
        self.mod = PILOT["qualify"].__globals__
        self.pod = {
            "metadata": {
                "name": "cpu-abc-123",
                "namespace": "sky-cpu",
                "uid": "pod-id",
                "ownerReferences": [
                    {"kind": "Job", "name": self.name, "uid": UID, "controller": True}
                ],
            },
            "spec": {"nodeName": "worker", "containers": [{"image": PILOT["IMAGE"]}]},
            "status": {
                "phase": "Succeeded",
                "containerStatuses": [
                    {
                        "name": "probe",
                        "restartCount": 0,
                        "imageID": "container-id",
                        "state": {"terminated": {"exitCode": 0}},
                    }
                ],
            },
        }
        self.log = json.dumps(
            {
                "pod_uid": "pod-id",
                "restricted": True,
                "dns": "resolved",
                "api_tcp": "blocked",
            }
        )

    def test_bound_job_and_admission_pin(self):
        path = ROOT / "platform/overlays/prod/sky-cpu/admission.yaml"
        policy = next(yaml.safe_load_all(path.read_text()))
        image = next(
            x["expression"]
            for x in policy["spec"]["variables"]
            if x["name"] == "nativeImage"
        )
        self.assertEqual(image, repr(PILOT["IMAGE"]))
        spec = self.obj["spec"]
        self.assertEqual(
            (spec["activeDeadlineSeconds"], spec["backoffLimit"]), (180, 0)
        )
        pod = spec["template"]["spec"]
        self.assertEqual(
            pod["affinity"]["nodeAffinity"][
                "requiredDuringSchedulingIgnoredDuringExecution"
            ]["nodeSelectorTerms"][0]["matchFields"],
            [{"key": "metadata.name", "operator": "In", "values": ["worker"]}],
        )
        self.assertEqual(
            pod["nodeSelector"], {"airaware.training/pool": "training-cpu"}
        )
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertEqual(pod["containers"][0]["image"], PILOT["IMAGE"])

    def test_dry_run_only_and_ambiguous_create(self):
        for execute in (False, True):
            calls = []

            def fake(client, *args, body=None):
                calls.append(args)
                if "--dry-run=server" in args:
                    return "ok"
                raise TimeoutError()

            with self.subTest(execute=execute), patch.dict(self.mod, {"kubectl": fake}):
                if execute:
                    with self.assertRaises(TimeoutError):
                        PILOT["qualify"]([], self.obj, "worker", True)
                    self.assertEqual(len(calls), 2)
                else:
                    self.assertFalse(
                        PILOT["qualify"]([], self.obj, "worker", False)["executed"]
                    )
                    self.assertEqual(len(calls), 1)
                self.assertTrue(all(args[0] == "create" for args in calls))

    def test_uid_bound_cleanup_even_when_pod_fails(self):
        for succeeds in (True, False):
            calls = []

            def fake(client, *args, body=None):
                calls.append((args, body))
                if args[0] == "create":
                    return (
                        "dry-run"
                        if "--dry-run=server" in args
                        else json.dumps({"metadata": {"uid": UID}})
                    )
                if args[:2] == ("get", "pods"):
                    self.assertIn("batch.kubernetes.io/controller-uid=" + UID, args)
                    items = (
                        [] if any(a[0][0] == "delete" for a in calls) else [self.pod]
                    )
                    return json.dumps({"items": items})
                if args[0] == "logs":
                    return self.log
                if args[0] == "delete":
                    self.assertEqual(body["preconditions"], {"uid": UID})
                    self.assertEqual(body["propagationPolicy"], "Foreground")
                    return "{}"
                self.assertEqual(args[:3], ("get", "job", self.name))
                return ""

            with (
                self.subTest(succeeds=succeeds),
                patch.dict(self.mod, {"kubectl": fake}),
                patch.object(subprocess, "run") as wait,
            ):
                if succeeds:
                    result = PILOT["qualify"]([], self.obj, "worker", True)
                    self.assertEqual(result["cleanup"], "verified")
                    self.assertEqual(result["pod_uid"], "pod-id")
                else:
                    wait.side_effect = subprocess.TimeoutExpired("wait", 225)
                    with self.assertRaises(subprocess.TimeoutExpired):
                        PILOT["qualify"]([], self.obj, "worker", True)
                self.assertEqual(sum(a[0] == "delete" for a, _ in calls), 1)

    def test_refuses_unrelated_pod_or_incomplete_runtime_proof(self):
        for node, owner, log in (
            ("another", UID, self.log),
            ("worker", "stale", self.log),
            ("worker", UID, self.log.replace("pod-id", "stale")),
            ("worker", UID, self.log.replace("blocked", "allowed")),
        ):
            self.pod["metadata"]["ownerReferences"][0]["uid"] = owner
            with (
                self.subTest(node=node, owner=owner, log=log),
                self.assertRaises(ValueError),
            ):
                PILOT["verify"](self.pod, log, self.name, UID, node)
