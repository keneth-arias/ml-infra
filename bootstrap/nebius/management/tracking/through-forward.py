# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["mlflow-skinny==3.18.0", "matplotlib==3.10.7"]
# ///
"""Run an existing report or readback against the pinned private tracking Service."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import runpy
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryFile
import time

import requests

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "workloads/sky"))
from private_api import pinned_kubeconfig_ca

HOST = "mlflow.tracking.svc.cluster.local"
FORWARD = re.compile(r"^Forwarding from 127[.]0[.]0[.]1:([0-9]+) -> 8443$", re.M)


@contextmanager
def local_forward(client):
    with TemporaryFile(mode="w+b") as log:
        process = subprocess.Popen(
            [*client, "-n", "tracking", "port-forward", "--address=127.0.0.1",
             "--pod-running-timeout=15s", "service/mlflow", ":https"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and process.poll() is None:
                # pread leaves the child's shared output offset unchanged.
                output = os.pread(log.fileno(), 4096, 0).decode(errors="replace")
                selected = FORWARD.search(output)
                if selected:
                    yield int(selected[1])
                    return
                time.sleep(0.1)
            raise RuntimeError("TLS port-forward did not become ready")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def configure_client(port, ca, bearer):
    address_lookup = socket.getaddrinfo

    def loopback_only(host, service, *args, **kwargs):
        if host != HOST or str(service) != str(port):
            raise socket.gaierror(socket.EAI_NONAME, "Only the private tracking forward is available")
        return address_lookup("127.0.0.1", service, *args, **kwargs)

    socket.getaddrinfo = loopback_only
    for key in tuple(os.environ):
        if key.upper().endswith("_PROXY") or key.startswith("MLFLOW_TRACKING_"):
            del os.environ[key]
    uri = f"https://{HOST}:{port}"
    os.environ.update(
        MLFLOW_TRACKING_URI=uri,
        MLFLOW_TRACKING_TOKEN=bearer,
        MLFLOW_TRACKING_SERVER_CERT_PATH=str(ca),
        MLFLOW_TRACKING_INSECURE_TLS="false",
        MLFLOW_HTTP_REQUEST_MAX_RETRIES="0",
        MLFLOW_HTTP_REQUEST_TIMEOUT="8",
        MLFLOW_DISABLE_TELEMETRY="true",
    )
    with requests.Session() as session:
        session.trust_env = False
        invalid = ("A" if bearer[0] != "A" else "B") + bearer[1:]
        headers = ({}, {"Authorization": f"Bearer {invalid}"}, {"Authorization": f"Bearer {bearer}"})
        statuses = []
        for auth in headers:
            with session.get(f"{uri}/health", headers=auth, allow_redirects=False,
                             verify=str(ca), timeout=5, stream=True) as response:
                statuses.append(response.status_code)
        if statuses != [401, 401, 200]:
            raise ValueError("Expected anonymous and invalid bearer denial and authenticated TLS health")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-kubeconfig", required=True, type=Path)
    parser.add_argument("--ca", required=True, type=Path)
    parser.add_argument("--bearer", required=True, type=Path)
    modes = parser.add_subparsers(dest="mode", required=True)
    report = modes.add_parser("report")
    report.add_argument("--run", action="append", required=True)
    report.add_argument("--output", required=True, type=Path)
    restore = modes.add_parser("restore-check")
    restore.add_argument("--read-receipt", type=Path, required=True)
    args = parser.parse_args()

    if any(file.stat().st_mode & 0o077 for file in (args.operator_kubeconfig, args.bearer)):
        raise ValueError("Expected owner-only operator kubeconfig and bearer files")
    bearer = args.bearer.read_text()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", bearer):
        raise ValueError("Expected a file-backed URL-safe tracking bearer")

    client = ["kubectl", "--kubeconfig", str(args.operator_kubeconfig), "--context", "prod-nebius"]
    pinned_kubeconfig_ca(
        client, os.environ["NEBIUS_TRAINING_CLUSTER_ID"], os.environ["NEBIUS_CA_BUNDLE_SHA256"],
        endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"), direct=True,
    )
    if args.mode == "report":
        entry = ROOT / "workloads/sky/direct-learning-curves.py"
        command = [part for run_id in args.run for part in ("--run", run_id)]
        command += ["--output", str(args.output)]
    else:
        entry = ROOT / "workloads/smoke/tracking-roundtrip.py"
        command = ["--read-receipt", str(args.read_receipt)]

    with local_forward(client) as port:
        configure_client(port, args.ca, bearer)
        sys.argv = [str(entry), *command]
        runpy.run_path(str(entry), run_name="__main__")


def bounded_stop(_signum, _frame):
    raise TimeoutError("Tracking management deadline reached")


if __name__ == "__main__":
    signal.signal(signal.SIGALRM, bounded_stop)
    signal.signal(signal.SIGTERM, bounded_stop)
    signal.alarm(180)
    try:
        main()
    except Exception as error:
        sys.exit(f"Tracking management failed ({type(error).__name__}).")
    finally:
        signal.alarm(0)
