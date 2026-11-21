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
        parity = stage / "reference-parity.json"
        subprocess.run(
            [sys.executable, "/opt/forecast/reference/check_per_series_reference.py",
             "--reference", "/opt/forecast/reference/utils.py", "--weights", "/base/weights", "--receipt", str(parity)],
            cwd=cwd, check=True, env={**os.environ, "PYTHONPATH": cwd},
        )
        trace = json.loads(parity.read_bytes())
        if (trace["protocol"] != "per-series-reference-parity-v1"
                or trace["reference_sha256"] != "c19db31d2ce6e84a4ce02c9a0fdfe0869c53775f77b04dd4be5c893afd8335e2"
                or trace["weights_manifest_sha256"] != "7f8562992f1cf3852ced581f331247c6420fc90292d45453424c77f0a29c199f"
                or len(trace["cases"]) != 2):
            raise ValueError("The parity receipt differs from the pinned reference and model")
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
        report["reference_parity_sha256"] = hashlib.sha256(parity.read_bytes()).hexdigest()
        (stage / "freeze.json").write_text(json.dumps(report, sort_keys=True) + "\n")
        os.rename(stage, final)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    collect(sys.argv[1], Path("/output"))
