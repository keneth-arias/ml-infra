"""Install the independently approved receipt pair in an attended, fenced fit state directory."""

import argparse
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from episode_job import approved_flamingo_evidence


def promote(source, state, pins, parity_pin):
    if state.is_symlink() or not state.is_dir():
        raise ValueError("Expected a scoped fit state directory")
    approved = approved_flamingo_evidence(source, pins, parity_pin)
    final = state / "flamingo-baselines"
    if final.is_dir() and not final.is_symlink():
        if {p.name for p in final.iterdir()} == set(approved) and approved_flamingo_evidence(final, pins, parity_pin) == approved:
            return
        raise ValueError("Existing baseline directory differs from approval")
    if any(state.iterdir()):
        raise ValueError("Initial promotion requires an empty prepared run state directory")
    with TemporaryDirectory(dir=state) as tmp:
        stage = Path(tmp)
        for name, raw in approved.items():
            (stage / name).write_bytes(raw)
        stage.rename(final)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("baseline", type=Path, help="Published baseline directory, including freeze.json")
    p.add_argument("state", type=Path, help="Pre-enrolled, writer-fenced directory mounted as /state in the fit")
    p.add_argument("--approved-baselines", nargs=2, required=True)
    p.add_argument("--approved-parity-sha256", required=True)
    a = p.parse_args()
    promote(a.baseline, a.state, a.approved_baselines, a.approved_parity_sha256)
