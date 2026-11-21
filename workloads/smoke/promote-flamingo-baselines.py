"""Install the independently approved receipt pair in an attended, fenced fit state directory."""

import argparse
import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory


def receipts(directory, pins):
    result = {}
    for pin in pins:
        name = pin + ".json"
        path = directory / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 2 * 1024**2:
            raise ValueError("Expected a bounded regular baseline receipt")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != pin:
            raise ValueError("Receipt differs from pre-fit approval")
        result[name] = raw
    return result


def promote(source, state, pins):
    if len(pins) != 2 or len(set(pins)) != 2 or not all(re.fullmatch(r"[a-f0-9]{64}", s) for s in pins):
        raise ValueError("Expected two distinct approved SHA-256 values in freeze order")
    if any(path.is_symlink() or not path.is_dir() for path in (source, state)):
        raise ValueError("Expected scoped source and state directories")
    if json.loads((source / "freeze.json").read_bytes())["baseline_receipts_sha256"] != pins:
        raise ValueError("Frozen pair differs from pre-fit approval")
    approved = receipts(source, pins)
    final = state / "flamingo-baselines"
    if final.is_dir() and not final.is_symlink():
        if {p.name for p in final.iterdir()} == set(approved) and receipts(final, pins) == approved:
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
    a = p.parse_args()
    promote(a.baseline, a.state, a.approved_baselines)
