#!/usr/bin/env python3
"""Publish a systemd worker-cleanup failure to the configured VictoriaLogs OTLP endpoint."""

from http.client import HTTPException, HTTPSConnection
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlsplit
import uuid

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.logs.v1.logs_pb2 import SEVERITY_NUMBER_ERROR


def payload(run, host, event_id, rehearsal):
    request = ExportLogsServiceRequest()
    batch = request.resource_logs.add()
    for key, value in (("service.name", "nebius-worker-cleanup"), ("host.name", host)):
        batch.resource.attributes.add(key=key).value.string_value = value
    record = batch.scope_logs.add().log_records.add(
        time_unix_nano=time.time_ns(),
        severity_number=SEVERITY_NUMBER_ERROR,
        severity_text="ERROR",
    )
    record.body.string_value = "Nebius worker cleanup failed"
    for key, value in (
        ("event_name", "worker-cleanup-failed"),
        ("run_id", run),
        ("event_id", event_id),
    ):
        record.attributes.add(key=key).value.string_value = value
    record.attributes.add(key="rehearsal").value.bool_value = rehearsal
    return request.SerializeToString()


def send(url, token, data):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/insert/opentelemetry/v1/logs"
    ):
        raise ValueError("Expected the HTTPS VictoriaLogs OTLP ingestion endpoint")
    # Use the system CA store. Send the bearer only to the configured origin.
    conn = HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=15)
    try:
        conn.request(
            "POST",
            parsed.path,
            body=data,
            headers={
                "Content-Type": "application/x-protobuf",
                "Authorization": "Bearer " + token,
            },
        )
        response = conn.getresponse()
        if response.status == 429 or 500 <= response.status < 600:
            raise OSError(f"VictoriaLogs HTTP {response.status}")
        if response.status != 200:
            raise ValueError(f"VictoriaLogs HTTP {response.status}")
        result = ExportLogsServiceResponse.FromString(response.read(65537))
        if result.partial_success.rejected_log_records:
            raise ValueError("VictoriaLogs rejected the cleanup event")
    finally:
        conn.close()


def main(run, *, host=None):
    event_id = os.environ.get("MONITOR_INVOCATION_ID") or uuid.uuid4().hex
    data = payload(
        run,
        host or socket.gethostname(),
        event_id,
        os.environ.get("CLEANUP_ALERT_REHEARSAL") == "1",
    )
    credentials = Path(os.environ["CREDENTIALS_DIRECTORY"])
    token = (credentials / "otlp-bearer").read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("Invalid VictoriaLogs bearer credential")
    for attempt in range(3):
        try:
            send(os.environ["CLEANUP_OTLP_LOGS_URL"], token, data)
        except (OSError, HTTPException):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
        else:
            print(
                f"VictoriaLogs accepted cleanup event {event_id} for {run}", flush=True
            )
            return


if __name__ == "__main__":
    main(sys.argv[1])
