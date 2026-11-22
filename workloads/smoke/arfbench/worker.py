"""Pinned, serial ARFBench head-to-head. Runs as the released image's worker UID."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def verify_tree(root, expected):
    paths = {}
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("unexpected input type: " + str(path))
        if path.is_file():
            paths[path.relative_to(root).as_posix()] = path
    if paths.keys() != expected.keys():
        raise ValueError("input file set differs from the pinned inventory")
    for name, path in paths.items():
        with path.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != expected[name]:
                raise ValueError("digest mismatch: " + name)


def arm(backend, pins, deps):
    from importlib.metadata import version
    from types import SimpleNamespace
    sys.path[:0] = [deps, "/bench", "/opt/forecast/src"]
    import torch
    from evaluation.opentslm.arfbench.backends import load
    from evaluation.opentslm.arfbench.protocol import load_manifest
    from evaluation.opentslm.arfbench.replay import DECODE, probe_manifest
    from evaluation.opentslm.arfbench.run import evaluate
    from opentslm.forecast_inference import configure_device

    configure_device(0)
    torch.manual_seed(0)
    args = SimpleNamespace(backend=backend, device="cuda", revision=None, backbone_revision=None,
                           checkpoint="/base" if backend == "per-series" else "/bench/legacy",
                           backbone="/bench/llama")
    net, info = load(args)
    info.update(seed=0, decoding=DECODE, device="cuda", image=pins["image"], revisions=pins["revisions"],
                parent_manifest_sha256=pins["full_manifest_canonical_sha256"],
                versions={k:version(k) for k in ("torch", "transformers", "safetensors")})
    for split, manifest in [("test", load_manifest("/bench/manifest.json")), ("swap", probe_manifest("swap"))]:
        if manifest["sha256"] != pins["splits"][split]:
            raise ValueError("frozen split changed: " + split)
        output = Path("/output") / backend / split
        output.mkdir(parents=True)
        evaluate(net, manifest, dict(info, split=split, manifest_sha256=manifest["sha256"]), output)


def collect(pins, script):
    output = Path("/output")
    if os.geteuid() != 1000 or output.stat().st_uid != 1000 or any(output.iterdir()):
        raise ValueError("expected a fresh output owned by worker UID 1000")
    if os.environ.get("FORECAST_SOURCE_REVISION") != pins["opentslm_commit"]:
        raise ValueError("unexpected OpenTSLM revision in the trainer image")
    verify_tree(Path("/bench"), pins["inputs"])
    verify_tree(Path("/base"), pins["gemma"])
    with TemporaryDirectory(dir="/tmp") as tmp:
        for path in sorted(Path("/bench/wheels").glob("*.whl")):
            with ZipFile(path) as wheel:
                wheel.extractall(tmp)
        for backend in ("legacy", "per-series"):
            subprocess.run([sys.executable, "-I", "-B", "-c", script, "arm", backend, json.dumps(pins), tmp],
                           check=True)
    sys.path.insert(0, "/bench")
    from evaluation.opentslm.arfbench.protocol import load_manifest
    from evaluation.opentslm.arfbench.replay import compare, probe_manifest, read_run

    report = {"pins": pins, "splits": {}}
    for split, manifest in [("test", load_manifest("/bench/manifest.json")), ("swap", probe_manifest("swap"))]:
        result = compare(manifest, *(read_run(output / backend / split) for backend in ("legacy", "per-series")))
        if result["paired"] != {"test":133, "swap":4}[split]:
            raise ValueError("incomplete paired coverage")
        report["splits"][split] = result
    failed = any(result[arm]["failures"] for result in report["splits"].values() for arm in ("first", "second"))
    report["status"] = "failed-generation" if failed else "complete"
    (output / "comparison.json").write_text(json.dumps(report, sort_keys=True, allow_nan=False) + "\n")
    if failed:
        raise ValueError("generation errors recorded in paired evidence")


if __name__ == "__main__":
    if sys.argv[1] == "arm":
        arm(sys.argv[2], json.loads(sys.argv[3]), sys.argv[4])
    else:
        # Retrieve this exact issued -c source for fresh interpreter processes.
        collect(json.loads(sys.argv[1]), sys.orig_argv[sys.orig_argv.index("-c") + 1])
