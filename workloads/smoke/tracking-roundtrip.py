"""Write and read a synthetic training artifact through the authenticated MLflow proxy."""
import argparse
from contextlib import redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from urllib.parse import urlsplit


EXPERIMENT = "airaware-pm25"
ARTIFACT = "tracking-probe.bin"
METRIC = "tracking_probe"
PURPOSE = "nebius-tracking-qualification"


def tracking_target():
    uri = os.environ["MLFLOW_TRACKING_URI"].rstrip("/")
    url = urlsplit(uri)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Use an authenticated HTTPS tracking URI without embedded credentials")
    if not os.environ.get("MLFLOW_TRACKING_TOKEN") or any(os.environ.get(key) for key in (
        "MLFLOW_TRACKING_USERNAME", "MLFLOW_TRACKING_PASSWORD", "MLFLOW_TRACKING_CLIENT_CERT_PATH",
    )):
        raise ValueError("Use the workload's bearer credential")
    if os.environ.get("MLFLOW_TRACKING_INSECURE_TLS", "false").lower() != "false":
        raise ValueError("The tracking certificate must be verified")
    return uri


def check_authentication(uri, experiment):
    import requests

    url = uri + "/api/2.0/mlflow/experiments/get-by-name"
    verify = os.environ.get("MLFLOW_TRACKING_SERVER_CERT_PATH") or True
    statuses = []
    with requests.Session() as session:
        session.trust_env = False
        for token in (None, secrets.token_hex(32)):
            headers = {"Authorization": "Bearer " + token} if token else {}
            with session.get(url, params={"experiment_name": experiment}, headers=headers,
                             allow_redirects=False, verify=verify, timeout=(5, 10), stream=True) as reply:
                statuses.append(reply.status_code)
    if any(status not in (401, 403) for status in statuses):
        raise ValueError(f"Expected a credential denial for both requests, received {statuses}")
    return statuses


def require_artifact_proxy(uri):
    location = urlsplit(uri)
    if (location.scheme != "mlflow-artifacts" or location.netloc
            or not location.path.startswith("/") or location.query or location.fragment):
        raise ValueError("Expected artifacts proxied by the same MLflow tracking server")


def readback(client, experiment_id, run_id, digest):
    run = client.get_run(run_id)
    if (run.info.status != "FINISHED" or run.info.experiment_id != experiment_id
            or run.data.tags.get("qualification") != PURPOSE
            or run.data.tags.get("artifact_sha256") != digest
            or run.data.metrics.get(METRIC) != 1.0):
        raise ValueError("Synthetic run metadata or metric differs from the receipt")
    require_artifact_proxy(run.info.artifact_uri)
    with tempfile.TemporaryDirectory() as folder:
        saved = Path(client.download_artifacts(run_id, ARTIFACT, dst_path=folder))
        if saved.stat().st_size != 64 or hashlib.sha256(saved.read_bytes()).hexdigest() != digest:
            raise ValueError("Downloaded synthetic artifact differs from the receipt")


def qualify(read_receipt):
    uri = tracking_target()
    name = os.environ.get("MLFLOW_TRACKING_EXPERIMENT", EXPERIMENT)
    statuses = check_authentication(uri, name)
    import mlflow
    from mlflow import MlflowClient

    mlflow.set_tracking_uri(uri)
    client = MlflowClient(tracking_uri=uri)
    if read_receipt:
        record = json.loads(read_receipt.read_bytes())
        run_id, digest = record["run_id"], record["artifact_sha256"]
        if not re.fullmatch("[a-f0-9]{32}", run_id) or not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("Invalid synthetic tracking receipt")
        experiment = client.get_experiment_by_name(name)
        if experiment is None or experiment.experiment_id != record["experiment_id"]:
            raise ValueError("Restored experiment differs from the recorded one")
    else:
        experiment = mlflow.set_experiment(name)
        require_artifact_proxy(experiment.artifact_location)
        payload = secrets.token_bytes(64)
        digest = hashlib.sha256(payload).hexdigest()
        run = client.create_run(experiment.experiment_id, run_name=PURPOSE,
                                tags={"qualification": PURPOSE, "artifact_sha256": digest})
        run_id = run.info.run_id
        try:
            require_artifact_proxy(run.info.artifact_uri)
            client.log_metric(run_id, METRIC, 1.0, step=0)
            with tempfile.TemporaryDirectory() as folder:
                sample = Path(folder) / ARTIFACT
                sample.write_bytes(payload)
                client.log_artifact(run_id, str(sample))
        except Exception:
            client.set_terminated(run_id, status="FAILED")
            raise
        client.set_terminated(run_id, status="FINISHED")
    readback(MlflowClient(tracking_uri=uri), experiment.experiment_id, run_id, digest)
    return {"run_id": run_id, "experiment_id": experiment.experiment_id, "artifact_sha256": digest,
                      "anonymous_http": statuses[0], "invalid_bearer_http": statuses[1],
                      "node": os.environ.get("TRACKING_PROBE_NODE"), "mode": "restore" if read_receipt else "roundtrip"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--read-receipt", type=Path, help="Verify an existing run after restoring the database and artifacts")
    args = parser.parse_args()
    try:
        # The SDK prints run links on termination. Keep stdout a single JSON receipt.
        with redirect_stdout(sys.stderr):
            result = qualify(args.read_receipt)
        print(json.dumps(result))
    except Exception as exc:
        # HTTP errors can include server-controlled text or sensitive request parameters.
        sys.exit(f"Tracking qualification failed ({type(exc).__name__})")
