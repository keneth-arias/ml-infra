import json
import ssl
import subprocess
import unittest
from copy import deepcopy
from http.client import HTTPSConnection
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

import mlx_host


class ForecastContractTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "checkpoint_sha256": mlx_host.CHECKPOINT_SHA256,
            "weights_sha256": mlx_host.WEIGHTS_SHA256,
            "history_sha256": "a" * 64,
            "window_ids": ["station-1", "station-2"],
            "histories": [[float(i) for i in range(24)], [2.5] * 24],
        }

    def test_valid_payload_preserves_window_order(self):
        mlx_host.validate_request(self.request)
        self.assertEqual(self.request["window_ids"], ["station-1", "station-2"])

    def test_wrong_release_or_invalid_history_hash(self):
        for field in ("checkpoint_sha256", "weights_sha256", "history_sha256"):
            self.assert_rejected(field, "invalid")

    def test_duplicate_ids_and_unmatched_rows_are_rejected(self):
        self.assert_rejected("window_ids", ["station-1", "station-1"])
        self.assert_rejected("histories", [[1.0] * 24])

    def test_nonfinite_and_non_numeric_readings_are_rejected(self):
        for reading in (float("nan"), float("inf"), 10**1000, "12.0", True):
            histories = deepcopy(self.request["histories"])
            histories[0][0] = reading
            self.assert_rejected("histories", histories)

    def test_forecast_must_have_six_finite_values_per_window(self):
        mlx_host.validate_forecast([[2.0] * 6] * 2, 2)
        for forecast in ([[2.0] * 5] * 2, [[2.0] * 6], [[float("inf")] * 6] * 2):
            with self.assertRaises(ValueError):
                mlx_host.validate_forecast(forecast, 2)

    def assert_rejected(self, key, value):
        request = {**self.request, key: value}
        with self.assertRaises(ValueError):
            mlx_host.validate_request(request)


class HttpsBridgeTests(unittest.TestCase):
    def test_https_auth_identity_and_response_shape(self):
        with TemporaryDirectory() as directory:
            cert = Path(directory) / "cert.pem"
            key = Path(directory) / "key.pem"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                "-out", str(cert), "-keyout", str(key),
            ], capture_output=True, check=True)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            client_context = ssl.create_default_context(cafile=str(cert))
            with mlx_host.ForecastServer(("127.0.0.1", 0), mlx_host.ForecastHandler) as server:
                server.tls = tls
                server.authorization = b"Bearer " + b"a" * 32
                server.bundle = Path(directory)
                server.device = "synthetic"
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    request = {
                        **mlx_host.IDENTITY, "history_sha256": "b" * 64,
                        "window_ids": ["window"], "histories": [[1.0] * 24],
                    }
                    headers = {"Content-Type": "application/json"}
                    with patch.object(mlx_host.subprocess, "run") as worker:
                        worker.return_value.stdout = "[[1,2,3,4,5,6]]"
                        status, _ = self.request(server, client_context, request, headers)
                        self.assertEqual(status, 401)
                        headers["Authorization"] = server.authorization.decode()
                        stale = {**request, "weights_sha256": "f" * 64}
                        status, _ = self.request(server, client_context, stale, headers)
                        self.assertEqual(status, 400)
                        worker.assert_not_called()
                        status, body = self.request(server, client_context, request, headers)
                        self.assertEqual(status, 200)
                        self.assertEqual(body, {
                            **mlx_host.IDENTITY, "history_sha256": "b" * 64,
                            "window_ids": ["window"], "forecast": [[1, 2, 3, 4, 5, 6]],
                        })
                        self.assertEqual(json.loads(worker.call_args.kwargs["input"]), request["histories"])
                finally:
                    server.shutdown()
                    thread.join()

    def request(self, server, context, body, headers):
        connection = HTTPSConnection("localhost", server.server_port, context=context, timeout=5)
        try:
            connection.request("POST", "/v1/forecast", body=json.dumps(body), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
