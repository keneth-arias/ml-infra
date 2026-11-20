"""Prepare a matched direct-control fit from the exact quantile training artifact."""

import argparse
from pathlib import Path
from runpy import run_path

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("quantile_bundle", type=Path)
    p.add_argument("bundle_sha256")
    p.add_argument("output", type=Path)
    a = p.parse_args()
    worker = run_path(
        str(Path(__file__).resolve().parents[2] / "runtime/episode_job.py")
    )
    print(worker["prepare_point_control"](a.quantile_bundle, a.bundle_sha256, a.output))
