import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)

path = (
    Path(__file__).resolve().parents[1]
    / "bootstrap/nebius/management/notify-cleanup.py"
)
spec = importlib.util.spec_from_file_location("notify_cleanup", path)
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class CleanupAlertTest(TestCase):
    def test_failure_has_run_host_and_unique_delivery_id(self):
        req = ExportLogsServiceRequest.FromString(
            notify.payload("run-1", "fw13pro", "test-event", True)
        )
        (batch,) = req.resource_logs
        attrs = {kv.key: kv.value.string_value for kv in batch.resource.attributes}
        self.assertEqual(attrs["host.name"], "fw13pro")
        (record,) = batch.scope_logs[0].log_records
        fields = {kv.key: kv.value for kv in record.attributes}
        self.assertEqual(fields["run_id"].string_value, "run-1")
        self.assertEqual(fields["event_id"].string_value, "test-event")
        self.assertTrue(fields["rehearsal"].bool_value)
        self.assertEqual(record.severity_text, "ERROR")

    def test_refuses_partial_success_and_redirect(self):
        url = "https://example.test:8443/insert/opentelemetry/v1/logs"
        partial = ExportLogsServiceResponse()
        partial.partial_success.rejected_log_records = 1
        for status, body in ((200, partial.SerializeToString()), (307, b"")):
            conn = Mock()
            conn.getresponse.return_value.status = status
            conn.getresponse.return_value.read.return_value = body
            with patch.object(notify, "HTTPSConnection", return_value=conn):
                with self.assertRaises(ValueError):
                    notify.send(url, "fixture-token", b"record")
                conn.request.assert_called_once()
                conn.close.assert_called_once()

    def test_missing_private_ca_refuses_before_sending_bearer(self):
        with patch.object(notify, "HTTPSConnection") as conn, self.assertRaises(FileNotFoundError):
            notify.send("https://192.168.50.155:8443/insert/opentelemetry/v1/logs",
                        "fixture", b"data", cafile="/missing/cleanup-fixture-ca.crt")
        conn.assert_not_called()

    def test_empty_preallocation_request_rejects_an_ingress_denial(self):
        conn = Mock()
        conn.getresponse.return_value.status = 403
        with patch.object(notify, "HTTPSConnection", return_value=conn), self.assertRaisesRegex(ValueError, "403"):
            notify.send("https://192.168.50.155:8443/insert/opentelemetry/v1/logs", "fixture", b"")
        conn.request.assert_called_once()
