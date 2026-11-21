"""Request cooperative fit shutdown from inside the identified training pod."""

import argparse
import json
import os
import re
import signal
import time
from pathlib import Path

from opentslm.forecast_checkpoint import files, read_manifest, sha256


def channel(run, step, attempt):
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,62}", run) or min(step, attempt) < 1:
        raise ValueError("Invalid fit attempt")
    return Path("/tmp") / f"episode-stop-{run}-{step}-{attempt}"


def arm(directory, stop_after=None):
    # An attempt has a single lifetime inside this pod. A retry uses a new attempt.
    directory.mkdir(mode=0o700)
    fd = os.open(directory / "requested", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)

    def notify(*_):
        os.pwrite(fd, b"1", 0)

    signal.signal(signal.SIGTERM, notify)
    if stop_after:
        signal.signal(signal.SIGALRM, notify)
        signal.alarm(stop_after)
    return directory / "requested"


def acknowledge(directory, status, generation=None):
    receipt = {"status": status}
    if generation:
        receipt.update(
            generation=generation.name,
            manifest_sha256=sha256(generation / "manifest.json"),
        )
    tmp = directory / "ack.tmp"
    tmp.write_text(json.dumps(receipt))
    tmp.replace(directory / "ack.json")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pod-uid", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--step", required=True, type=int)
    p.add_argument("--attempt", required=True, type=int)
    p.add_argument("--timeout", type=int, default=60)
    a = p.parse_args()
    if (
        not a.pod_uid
        or a.pod_uid != os.environ.get("EPISODE_POD_UID")
        or not 0 < a.timeout <= 90
    ):
        p.error("Expected this pod's verified UID and a timeout of at most 90 seconds")
    directory = channel(a.run_id, a.step, a.attempt)
    with (directory / "requested").open("r+b") as request:
        request.write(b"1")
    deadline = time.monotonic() + a.timeout
    ack = directory / "ack.json"
    while not ack.exists():
        if time.monotonic() >= deadline:
            p.exit(1, "No shutdown acknowledgement before timeout\n")
        time.sleep(0.2)
    receipt = json.loads(ack.read_text())
    if receipt["status"] in {"interrupted", "complete"}:
        generation = Path("/state") / receipt["generation"]
        manifest = read_manifest(generation)
        if (
            not manifest
            or sha256(generation / "manifest.json") != receipt["manifest_sha256"]
            or manifest["files"] != files(generation)
        ):
            p.exit(1, "Acknowledged generation failed verification\n")
    elif receipt["status"] not in {"stopped-before-launch", "stopped-before-update"}:
        p.exit(1, "Unknown acknowledgement\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
