"""Run inside the released trainer on the enrolled GPU. Publish only a complete pair."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def collect(image_digest, output):
    from episode_job import approved_flamingo_evidence

    final = output / "baseline"
    if final.exists() or final.is_symlink():
        raise FileExistsError("Baseline already published")
    cli = [sys.executable, "-m", "opentslm.flamingo_qualification"]
    # Import the paired forecast checkout from the read-only image.
    cwd = "/opt/forecast/src"
    with TemporaryDirectory(dir=output) as staging:
        stage = Path(staging)
        parity = stage / "reference-parity.json"
        # The upstream parity CLI loads directly on CUDA. Apply the Job's
        # allocator limit in that same process before either model loads.
        parity_main = (
            "import runpy, sys\n"
            "from opentslm.forecast_inference import configure_device\n"
            "configure_device(0)\n"
            "sys.argv = sys.argv[1:]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')"
        )
        subprocess.run(
            [sys.executable, "-c", parity_main, "/opt/forecast/reference/check_per_series_reference.py",
             "--reference", "/opt/forecast/reference/utils.py", "--weights", "/base/weights", "--receipt", str(parity)],
            cwd=cwd, check=True, env={**os.environ, "PYTHONPATH": cwd},
        )
        trace = json.loads(parity.read_bytes())
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
        # Bind the comparison to this issued Job image and the exact language
        # freeze before hashing. The signed release supplies the digest at run time.
        trace.update(image_digest=image_digest, language_freeze=report)
        parity.write_text(json.dumps(trace, sort_keys=True, allow_nan=False) + "\n")
        report["reference_parity_sha256"] = hashlib.sha256(parity.read_bytes()).hexdigest()
        (stage / "freeze.json").write_text(json.dumps(report, sort_keys=True) + "\n")
        approved_flamingo_evidence(stage, pins, report["reference_parity_sha256"])
        os.rename(stage, final)
    print(json.dumps(report, sort_keys=True), flush=True)


if __name__ == "__main__":
    sys.path.insert(0, "/opt/ml-infra")
    output = Path("/output")
    if os.geteuid() != 1000 or output.stat().st_uid != 1000 or any(output.iterdir()):
        raise ValueError("Expected the staged, empty baseline output owned by UID 1000")
    collect(sys.argv[1], output)
