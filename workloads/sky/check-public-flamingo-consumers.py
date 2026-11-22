#!/usr/bin/env python3
"""Observe the closed managed namespace before issuing one public source inventory."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

from private_api import pinned_kubeconfig_ca
import public_flamingo_stage_guard as guard
import public_flamingo_window as windows


HERE = Path(__file__).resolve().parent
CLUSTER = runpy.run_path(str(HERE / "render-public-flamingo-stage.py"))["CLUSTER"]
NS = "training"
PIN_KEYS = {"policy_uid", "binding_uid", "policy_generation", "binding_generation"}


def observe(run, pins, operator, get, list_pods, secret_absent, window=None):
    windows.validate_window_run(window, run)
    windows.validate_window_pin(window, pins)
    if set(pins) != (PIN_KEYS | {"window_sha256"} if window else PIN_KEYS):
        raise ValueError("Expected the four independently enrolled closed guard pins")
    secret = "stage-" + run + "-inventory"

    def closed():
        if guard.verify_guard(get, run, "closed", pins, window=window) != pins:
            raise ValueError("Closed guard identity or generation differs from approval")

    closed()
    if not secret_absent(secret):
        raise ValueError("Inventory Secret exists and requires owner reconciliation")
    pods = list_pods()
    if (pods["apiVersion"] != "v1" or pods["kind"] != "PodList" or not pods["metadata"].get("resourceVersion")
            or pods["metadata"].get("continue") or pods["metadata"].get("remainingItemCount", 0) != 0
            or not isinstance(pods["items"], list)):
        raise ValueError("Expected a complete training Pod inventory")
    for pod in pods["items"]:
        if pod["metadata"]["namespace"] != NS:
            raise ValueError("Pod inventory differs from the enrolled namespace")
        if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} and guard.preexisting_secret_consumer(pod, secret):
            raise ValueError("A live Pod references the proposed inventory Secret")
    if not secret_absent(secret):
        raise ValueError("Inventory Secret appeared during the consumer check")
    closed()
    return {**({"window_sha256": window.sha256} if window else {}),
            "schema": "public-flamingo-consumer-check-v1", "cluster_id": CLUSTER, "run": run,
            "storage_operator": operator, "inventory_secret": secret, "closed_guard": pins,
            "observed_at": datetime.now(timezone.utc).isoformat(), "no_live_consumers": True,
            "secret_absent": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--closed-pins", type=Path, required=True)
    parser.add_argument("--closed-pins-sha256", required=True)
    windows.add_window_args(parser)
    a = parser.parse_args()
    window = windows.load_window(a, a.run, live=True)
    closeout = runpy.run_path(str(HERE / "public-flamingo-stage-closeout.py"))
    pins = closeout["pinned_file"](a.closed_pins, a.closed_pins_sha256)
    profile = Path(os.environ["NEBIUS_STORAGE_KUBECONFIG"])
    operator = os.environ["NEBIUS_STORAGE_OPERATOR_IDENTITY"]
    if (not operator or operator in ("system:anonymous", "system:serviceaccount:training:episode-submit")
            or profile.stat().st_mode & 0o077 or os.environ["NEBIUS_TRAINING_CLUSTER_ID"] != CLUSTER):
        raise ValueError("Expected the independently enrolled managed storage observer")
    kube = ["kubectl", "--kubeconfig", str(profile), "--context", "prod-nebius"]
    pinned_kubeconfig_ca(kube, CLUSTER, os.environ["NEBIUS_CA_BUNDLE_SHA256"],
                         endpoint=os.environ.get("NEBIUS_API_ENDPOINT", "private"), direct=True)

    def read(*cmd):
        return subprocess.check_output([*kube, "--request-timeout=10s", *cmd], timeout=15, stderr=subprocess.DEVNULL)
    if read("auth", "whoami", "-o", "jsonpath={.status.userInfo.username}").decode() != operator:
        raise ValueError("Unexpected managed storage observer identity")
    def get(*args):
        return json.loads(read("get", *args, "-o", "json"))
    def list_pods():
        return json.loads(read("get", "--raw=/api/v1/namespaces/training/pods"))
    def secret_absent(name):
        # A 403 or transport error exits nonzero. A successful 404 suppression alone is empty.
        return not read("get", "secret", name, "-n", NS, "--ignore-not-found", "-o", "name").strip()
    print(json.dumps(observe(a.run, pins, operator, get, list_pods, secret_absent, window), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        sys.exit("Inventory consumer check failed: " + (str(error) if type(error) is ValueError else type(error).__name__))
