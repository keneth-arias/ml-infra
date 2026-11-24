"""Serve the enrolled Neblima forecast bundle on native macOS Metal."""

import argparse
import hashlib
import hmac
import importlib.util
import json
import platform
import re
import ssl
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import monotonic

CHECKPOINT_SHA256 = "ca2ee75b0702a7893bc76a196351a45900fd54b68347f724157a532062d5c0cf"
WEIGHTS_SHA256 = "9a29ea6d5608d1ce9bc47b0bbaaaf316319988fe3091cab30b40148cb105be43"
BUNDLE_FILES = {
    "model.safetensors": WEIGHTS_SHA256,
    "base-config.json": "b90b86f35c8e6925ef74ee04d0e758f0a845c83a42089ad82bbaa948de9b4204",
    "config.json": "7a81996fbd25c310d35738d49593086f7f2424d207411fef5fc0b8d8a465e5bd",
    "mlx_forecast.py": "cb6e51f08f1f0a2508c0fbbfc68780ff418f163c74f0a3bb0c4e0a5c379e9b8a",
    "scaler.json": "c8ebd87bce74ea0157e85093e4c9c81d981af34fb62f1cbc2c12ce653b2c3863",
}
IDENTITY = {"checkpoint_sha256": CHECKPOINT_SHA256, "weights_sha256": WEIGHTS_SHA256}
MAX_BODY = 128 * 1024
MAX_WINDOWS = 56
MAX_FLOAT32 = 3.4028234663852886e38


def validate_rows(rows, count, width):
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError("Unexpected row count")
    for row in rows:
        if (not isinstance(row, list) or len(row) != width or
                any(type(value) not in (float, int) or not -MAX_FLOAT32 <= value <= MAX_FLOAT32
                    for value in row)):
            raise ValueError("Unexpected row shape or value")


def validate_request(payload):
    fields = set(IDENTITY) | {"history_sha256", "window_ids", "histories"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("Unexpected request fields")
    if any(payload[key] != value for key, value in IDENTITY.items()):
        raise ValueError("Unexpected forecast identity")
    digest = payload["history_sha256"]
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("Unexpected history digest")
    ids = payload["window_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_WINDOWS or
            any(not isinstance(key, str) or not 1 <= len(key) <= 256 or not key.isprintable() for key in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("Unexpected window IDs")
    validate_rows(payload["histories"], len(ids), 24)


def validate_forecast(forecast, count):
    validate_rows(forecast, count, 6)


def distinct_keys(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("Duplicate request field")
    return result


def metal():
    import mlx.core as mx

    if platform.system() != "Darwin" or platform.machine() != "arm64" or not mx.metal.is_available():
        raise RuntimeError("Apple Silicon Metal is required")
    mx.set_default_device(mx.gpu)
    return mx.device_info(mx.gpu)["device_name"]


def infer(bundle, histories):
    metal()
    spec = importlib.util.spec_from_file_location("neblima_forecast", bundle / "mlx_forecast.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = module.load_bundle(bundle)
    values = []
    for start in range(0, len(histories), 16):
        values.extend(model.predict(histories[start:start + 16]).tolist())
    return values


class ForecastHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        # Keep request metadata, paths and auth headers out of logs.
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        headers = self.headers.get_all("Authorization", [])
        if len(headers) == 1 and hmac.compare_digest(headers[0].encode("latin-1"), self.server.authorization):
            return True
        self.reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def do_GET(self):
        if self.authenticated():
            if self.path == "/healthz":
                self.reply(HTTPStatus.OK, {**IDENTITY, "device": self.server.device})
            else:
                self.reply(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if not self.authenticated():
            return
        if self.path != "/v1/forecast":
            self.reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            sizes = self.headers.get_all("Content-Length", [])
            if (self.headers.get_content_type() != "application/json" or
                    self.headers.get("Transfer-Encoding") is not None or len(sizes) != 1 or
                    not re.fullmatch("[0-9]{1,6}", sizes[0]) or not 1 <= int(sizes[0]) <= MAX_BODY):
                raise ValueError("Expected bounded JSON body")
            payload = json.loads(self.rfile.read(int(sizes[0])), object_pairs_hook=distinct_keys)
            validate_request(payload)
        except (ValueError, RecursionError, TimeoutError):
            self.reply(HTTPStatus.BAD_REQUEST, {"error": "invalid forecast request"})
            return
        started = monotonic()
        try:
            process = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--worker", str(self.server.bundle)],
                input=json.dumps(payload["histories"], allow_nan=False), capture_output=True,
                text=True, timeout=90, check=True,
            )
            forecast = json.loads(process.stdout)
            validate_forecast(forecast, len(payload["window_ids"]))
        except (subprocess.SubprocessError, OSError, ValueError) as error:
            print(f"Metal forecast failed: {type(error).__name__}", file=sys.stderr, flush=True)
            self.reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "forecast failed"})
            return
        print(f"Metal forecast windows={len(forecast)} seconds={monotonic() - started:.3f}", flush=True)
        payload.pop("histories")
        self.reply(HTTPStatus.OK, {**payload, "forecast": forecast})


class ForecastServer(HTTPServer):
    def get_request(self):
        sock, address = super().get_request()
        try:
            sock.settimeout(8)
            return self.tls.wrap_socket(sock, server_side=True), address
        except OSError:
            sock.close()
            raise

    def handle_error(self, request, client_address):
        # A disconnected client must never expose request data in a traceback.
        print("Metal forecast connection closed unexpectedly", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--weights-sha256", required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18443)
    args = parser.parse_args()
    bundle = args.bundle.resolve(strict=True)
    if args.weights_sha256 != WEIGHTS_SHA256:
        raise ValueError("Only the enrolled FP32 export is supported")
    for filename, pinned in BUNDLE_FILES.items():
        with (bundle / filename).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != pinned:
                raise ValueError(f"Bundle verification failed for {filename}")
    token = args.token_file.read_bytes().strip()
    if not re.fullmatch(rb"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Expected a 32-128 character URL-safe bearer token")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(args.cert, args.key)
    device = metal()
    with ForecastServer((args.host, args.port), ForecastHandler) as server:
        server.tls = tls
        server.authorization = b"Bearer " + token
        server.bundle = bundle
        server.device = device
        print(f"MLX host ready on {device} at {args.host}:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        print(json.dumps(infer(Path(sys.argv[2]), json.load(sys.stdin)), allow_nan=False))
    else:
        main()
