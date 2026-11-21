"""Run inside the released trainer on the enrolled GPU. Publish only a complete pair."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def collect(image_digest, output):
    final = output / "baseline"
    if final.exists() or final.is_symlink():
        raise FileExistsError("Baseline already published")
    cli = [sys.executable, "-m", "opentslm.flamingo_qualification"]
    # Import the paired forecast checkout from the read-only image.
    cwd = "/opt/forecast/src"
    with TemporaryDirectory(dir=output) as staging:
        stage = Path(staging)
        paths = [stage / "base-1.json", stage / "base-2.json"]
        for path in paths:
            subprocess.run([*cli, "base", "--weights", "/base/weights", "--image-digest", image_digest,
                            "--output", str(path)], cwd=cwd, check=True)
        raw = subprocess.check_output([*cli, "freeze", "--baselines", *map(str, paths)], cwd=cwd)
        pins = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        report = json.loads(raw)
        if pins != report["baseline_receipts_sha256"] or pins[0] == pins[1]:
            raise ValueError("Freeze did not identify these two fresh-process receipts")
        for path, pin in zip(paths, pins):
            path.rename(stage / (pin + ".json"))
        (stage / "freeze.json").write_bytes(raw)
        os.rename(stage, final)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    collect(sys.argv[1], Path("/output"))
