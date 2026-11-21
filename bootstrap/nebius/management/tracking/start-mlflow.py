"""Start the loopback tracking server using only the operator-approved artifact destination."""

import os
from pathlib import Path
import re
from urllib.parse import quote


def main():
    destination = os.environ["MLFLOW_ARTIFACTS_DESTINATION"]
    if not re.fullmatch(r"s3://[a-z0-9][a-z0-9-]{1,61}[a-z0-9]/[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", destination):
        raise ValueError("Expected a dedicated Nebius artifact bucket and explicit prefix")
    password = Path("/run/tracking/db/password").read_text()
    if not password or password != password.strip():
        raise ValueError("Expected a nonempty PostgreSQL password without surrounding whitespace")
    restore_mode = os.environ.get("MLFLOW_RESTORE_DB", "false")
    if restore_mode not in {"true", "false"}:
        raise ValueError("Expected MLFLOW_RESTORE_DB to be true or false")
    database = "mlflow_restore" if restore_mode == "true" else "mlflow"
    encoded = quote(password, safe="")
    os.environ["MLFLOW_BACKEND_STORE_URI"] = (
        f"postgresql://{database}:{encoded}@postgres.tracking.svc.cluster.local:5432/{database}"
    )
    os.execvp("mlflow", [
        "mlflow", "server", "--host", "127.0.0.1", "--port", "5000", "--workers", "1",
        "--allowed-hosts", "mlflow.tracking.svc.cluster.local,127.0.0.1:*",
        "--serve-artifacts", "--artifacts-destination", destination,
    ])


if __name__ == "__main__":
    main()
