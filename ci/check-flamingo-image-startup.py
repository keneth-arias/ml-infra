"""Exercise the staged programs as the production UID without loading any weights."""

import os
from pathlib import Path
import runpy
import sys


def main(role):
    if (os.geteuid(), os.getegid()) != (1000, 1000) or not sys.flags.isolated:
        raise RuntimeError("Use the worker UID, GID and isolated Python")
    sys.path[:0] = ["/opt/ml-infra", "/opt/forecast/src"]
    import episode_job
    import stage_episode_inputs
    from opentslm import forecast_inputs

    if role == "preflight":
        return
    if role != "trainer":
        raise ValueError("Expected trainer or preflight")

    from opentslm.flamingo_qualification import protocol

    reference = Path("/opt/forecast/reference")
    parity = runpy.run_path(str(reference / "check_per_series_reference.py"))
    parity["reference_module"](reference / "utils.py")
    (reference / "LICENSE").read_text()
    if protocol()["base_manifest_sha256"] != episode_job.FLAMINGO_WEIGHTS:
        raise ValueError("Qualification fixture and runtime disagree on the base")
    runpy.run_path("/opt/forecast/train_qwen_forecast.py")


if __name__ == "__main__":
    main(sys.argv[1])
