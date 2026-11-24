"""Render the macOS LaunchAgent for the pinned native MLX bridge."""

import argparse
import plistlib
import sys
from pathlib import Path

from mlx_host import WEIGHTS_SHA256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.expanduser().absolute()
    bundle = args.bundle.expanduser().absolute()
    # Keep the venv executable path, which can be a symlink to a global Python.
    python = args.python.expanduser().absolute()
    plistlib.dump({
        "Label": "lat.orbitly.neblima-mlx",
        "ProgramArguments": [
            str(python), "-u", str(root / "bin" / "mlx_host.py"),
            "--bundle", str(bundle),
            "--weights-sha256", WEIGHTS_SHA256,
            "--cert", str(root / "tls" / "cert.pem"),
            "--key", str(root / "tls" / "key.pem"),
            "--token-file", str(root / "tls" / "token"),
            "--host", "127.0.0.1", "--port", "18443",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "EnvironmentVariables": {"PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": str(root / "logs" / "out.log"),
        "StandardErrorPath": str(root / "logs" / "err.log"),
    }, sys.stdout.buffer)


if __name__ == "__main__":
    main()
