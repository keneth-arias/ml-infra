import json
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch

import yaml

from cpu_admission_receipt import check_image


class CpuAdmissionTest(unittest.TestCase):
    def test_policy_matches_only_qualified_cpu_index(self):
        repository = "ghcr.io/keneth-arias/ml-infra-sky-cpu"
        index = "sha256:" + "a" * 64
        receipt = {"role": "rootless-cpu", "repository": repository, "index": index}
        variable = {"name": "skyImage", "expression": repr(repository + "@" + index)}
        policy = {"spec": {"variables": [variable]}}
        check_image(policy, receipt)
        for field, value in (("role", "trainer"), ("repository", "example.invalid/worker"),
                             ("index", "sha256:" + "b" * 64)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                check_image(policy, dict(receipt, **{field: value}))
        policy["spec"]["variables"].append(variable)
        with self.assertRaises(ValueError):
            check_image(policy, receipt)
        policy["spec"]["variables"] = [dict(variable, expression=repr(repository + "@sha256:" + "f" * 64))]
        with self.assertRaisesRegex(ValueError, "differs"):
            check_image(policy, receipt)


class CpuProbeIdentityTest(unittest.TestCase):
    def test_probe_refuses_identity_drift_before_contacting_api(self):
        root = Path(__file__).resolve().parents[1]
        check = runpy.run_path(str(root / "workloads/smoke/check-cpu-admission.py"))["check"]
        task = (root / "bootstrap/nebius/management/rootless/qualification-task.yaml").read_text()
        receipt = json.loads((root / "bootstrap/nebius/releases/rootless-candidate.json").read_text())
        image = receipt["repository"] + "@" + receipt["index"]
        for identity, override in (("SERVICE_ACCOUNT", None), ("NONE", "serviceAccountName"),
                                   ("NONE", "serviceAccount")):
            with self.subTest(identity=identity, override=override), patch("subprocess.run") as call:
                doc = yaml.safe_load(task)
                config = doc["config"]["kubernetes"]
                config["remote_identity"] = identity
                if override:
                    config["pod_config"]["spec"][override] = "skypilot-service-account"
                with self.assertRaisesRegex(ValueError, "NONE identity"):
                    check(["kubectl"], "sky-cpu", image, yaml.safe_dump(doc))
                call.assert_not_called()
