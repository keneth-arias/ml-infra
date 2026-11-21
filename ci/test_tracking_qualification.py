import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

SMOKE = Path(__file__).resolve().parents[1] / "workloads/smoke"
WORKER = runpy.run_path(str(SMOKE / "tracking-roundtrip.py"))
LAUNCH = runpy.run_path(str(SMOKE / "check-tracking-job.py"))
IMAGE = "ghcr.io/keneth-arias/ml-infra-qwen@sha256:" + "a" * 64


class TrackingQualificationTest(unittest.TestCase):
    def test_only_https_bearer_and_same_server_artifact_proxy(self):
        target = WORKER["tracking_target"]
        valid = {"MLFLOW_TRACKING_URI": "https://tracking.svc/", "MLFLOW_TRACKING_TOKEN": "fixture"}
        with patch.dict(os.environ, valid, clear=True):
            self.assertEqual(target(), "https://tracking.svc")
        for changed in ({"MLFLOW_TRACKING_URI": "http://tracking.svc"},
                        {"MLFLOW_TRACKING_URI": "https://operator:fixture@tracking.svc"},
                        {"MLFLOW_TRACKING_URI": "https://tracking.svc/?key=fixture"},
                        {"MLFLOW_TRACKING_INSECURE_TLS": "true"},
                        {"MLFLOW_TRACKING_USERNAME": "another"}, {"MLFLOW_TRACKING_TOKEN": ""}):
            with self.subTest(changed=changed), patch.dict(os.environ, valid | changed, clear=True), self.assertRaises(ValueError):
                target()
        proxy = WORKER["require_artifact_proxy"]
        proxy("mlflow-artifacts:/1/runs/example/artifacts")
        for uri in ("s3://bucket/objects", "file:///data", "mlflow-artifacts://elsewhere/path"):
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                proxy(uri)

    def test_artifact_endpoint_requires_credential_denial_without_redirects(self):
        url = "https://tracking.svc/api/2.0/mlflow-artifacts/artifacts/1/sample.bin"
        for codes in ((401, 403), (401, 200), (302, 401)):
            requests = MagicMock()
            session = requests.Session.return_value.__enter__.return_value
            responses = [MagicMock() for _ in codes]
            for response, code in zip(responses, codes):
                response.__enter__.return_value.status_code = code
            session.get.side_effect = responses
            with self.subTest(codes=codes), patch.dict(sys.modules, requests=requests):
                if codes == (401, 403):
                    self.assertEqual(WORKER["denied_without_bearer"](url), list(codes))
                else:
                    with self.assertRaisesRegex(ValueError, "credential denial"):
                        WORKER["denied_without_bearer"](url)
            first, second = session.get.call_args_list
            self.assertEqual(first.args, (url,))
            self.assertEqual(second.args, (url,))
            self.assertEqual(first.kwargs["headers"], {})
            self.assertRegex(second.kwargs["headers"]["Authorization"], r"^Bearer [a-f0-9]{64}$")
            self.assertFalse(first.kwargs["allow_redirects"])
            self.assertFalse(session.trust_env)

    def test_worker_placement_and_secret_bindings(self):
        for pool in ("cpu", "gpu"):
            job = LAUNCH["render"](IMAGE, "reviewed-node", pool, "tracking-check-test")
            spec = job["spec"]
            pod = spec["template"]["spec"]
            c, = pod["containers"]
            self.assertEqual(c["image"], IMAGE)
            self.assertEqual(c["command"][-1], (SMOKE / "tracking-roundtrip.py").read_text())
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual([spec["backoffLimit"], spec["activeDeadlineSeconds"]], [0, 300])
            self.assertEqual(pod["nodeSelector"], {"airaware.training/pool": "training-" + pool})
            self.assertEqual(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
                             ["nodeSelectorTerms"][0]["matchFields"][0]["values"], ["reviewed-node"])
            self.assertEqual("tolerations" in pod, pool == "gpu")
            self.assertNotIn("nvidia.com/gpu", c["resources"]["limits"])
            self.assertNotIn("nvidia.com/gpu", c["resources"]["requests"])
            secret = [e["valueFrom"]["secretKeyRef"] for e in c["env"] if "secretKeyRef" in e.get("valueFrom", {})]
            self.assertEqual(secret, [{"name":"episode-tracking", "key":"token"}])
            plain_env = {e["name"]: e["value"] for e in c["env"] if "value" in e}
            self.assertEqual(plain_env["MLFLOW_HTTP_REQUEST_TIMEOUT"], "5")
            self.assertEqual(plain_env["MLFLOW_HTTP_REQUEST_MAX_RETRIES"], "0")

    def test_dry_run_and_release_rotation(self):
        main = LAUNCH["main"]
        node = {"metadata":{"name":"worker","labels":{"airaware.training/pool":"training-cpu"}},
                "spec":{"providerID":"nebius://test"}, "status":{"conditions":[{"type":"Ready","status":"True"}]}}
        target = Mock(return_value=["scoped-kubectl"])
        release = ({"trainer": IMAGE}, "a" * 64)
        with (
            patch.dict(os.environ, {"NEBIUS_STAGING_OPERATOR_IDENTITY": "operator"}),
            patch.object(sys, "argv", ["probe", "--node", "worker", "--pool", "cpu"]),
            patch.dict(main.__globals__, kubectl_client_for_identity=target),
        ):
            for rotated in (False, True):
                calls = Mock(side_effect=[json.dumps(node), '{"metadata":{}}'])
                current = Mock(side_effect=[release, (release[0], "b"*64) if rotated else release])
                with (self.subTest(rotated=rotated), patch.dict(main.__globals__, current_release=current),
                      patch("subprocess.check_output", calls), contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO())):
                    if rotated:
                        with self.assertRaisesRegex(ValueError, "Deployment changed"):
                            main()
                        self.assertEqual(calls.call_count, 1)
                    else:
                        main()
                        command = calls.call_args.args[0]
                        self.assertIn("--dry-run=server", command)
                        self.assertEqual(command[0], "scoped-kubectl")
                    target.assert_called_with("training", "operator", direct=True)
