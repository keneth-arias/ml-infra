import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bootstrap/nebius"))
from control_storage import publish_timer_proof


class TimerStorageTests(unittest.TestCase):
    def test_proofs_use_host_prefixes_in_the_management_bucket(self):
        scope = dict(project_id="production", control_project_id="management",
                     state_bucket="state", control_bucket="controls", run_id="run", stage="gpu")
        for host in ("bm", "fw13"):
            with self.subTest(host=host):
                s3 = Mock()
                s3.put_object.return_value = {"VersionId": "published"}
                version = publish_timer_proof(s3, scope, host, {"host": host})
                self.assertEqual(version, "published")
                record = s3.put_object.call_args.kwargs
                self.assertEqual(record["Bucket"], "controls")
                self.assertEqual(record["Key"], f"prod/mk8s/release-control/timers/{host}/run/gpu.json")
                self.assertEqual(json.loads(record["Body"]), dict(
                    host=host, project_id="production", control_project_id="management",
                    state_bucket="state", control_bucket="controls",
                ))

    def test_both_timer_writes_refuse_missing_or_shared_control_storage(self):
        for host in ("bm", "fw13"):
            for extra in ({}, {"control_bucket": "state"}):
                with self.subTest(host=host, extra=extra):
                    s3 = Mock()
                    scope = dict(project_id="production", control_project_id="management",
                                 state_bucket="state", run_id="run", stage="gpu", **extra)
                    with self.assertRaisesRegex(ValueError, "control_bucket"):
                        publish_timer_proof(s3, scope, host, {"host": host})
                    s3.put_object.assert_not_called()
