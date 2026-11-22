"""Build the private read-only ARFBench inputs from local pinned artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from worker import verify_tree

HERE = Path(__file__).resolve().parent


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def project(raw, expected):
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("different frozen ARFBench release")
    parent = json.loads(raw)
    upstream = parent.pop("sha256")
    if hashlib.sha256(canonical(parent)).hexdigest() != upstream:
        raise ValueError("changed ARFBench canonical digest")
    rows = [r for r in parent["rows"] if r["split"] == "test"]
    for r in rows:
        if r["exclusion"]:
            r["series"] = []
    used = {key for r in rows for key in r["series"]}
    view = {key: parent[key] for key in ("schema", "dataset", "version", "max_values", "max_series")}
    view.update(parent_sha256=upstream, rows=rows, traces={key:parent["traces"][key] for key in used})
    view["sha256"] = hashlib.sha256(canonical(view)).hexdigest()
    return canonical(view)


def prepare(manifest, repo, snapshots, wheels, target):
    pins = json.loads((HERE / "pins.json").read_bytes())
    if target.exists():
        raise ValueError("choose a fresh private bundle directory")
    with TemporaryDirectory(dir=target.parent) as tmp:
        root = Path(tmp) / "inputs"
        root.mkdir()
        for name in pins["inputs"]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == "manifest.json":
                path.write_bytes(project(manifest.read_bytes(), pins["full_manifest_file_sha256"]))
            elif name.startswith("evaluation/"):
                raw = subprocess.check_output(["git", "-C", repo, "show", pins["opentslm_commit"] + ":" + name])
                path.write_bytes(raw)
            else:
                src = wheels / path.name if name.startswith("wheels/") else snapshots / name
                shutil.copyfile(src, path)
        verify_tree(root, pins["inputs"])
        root.rename(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("manifest", "opentslm", "snapshots", "wheels", "output"):
        parser.add_argument("--" + arg, type=Path, required=True)
    a = parser.parse_args()
    prepare(a.manifest, a.opentslm, a.snapshots, a.wheels, a.output)
