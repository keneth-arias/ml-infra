#!/usr/bin/env python3
"""Adapt only a dedicated SkyPilot 0.14.3 server's Kubernetes template for one rootless CPU head."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

STOCK = "ddd468efa7a6fd76fd601ab41324433d8130ecbad83bc01627d51e3ec246f62a"
POD = """          command: ["/usr/bin/tini", "--", "/bin/bash", "-ec"]
          args:
            - |
              rm -f /tmp/sky-cpu-ready
              mkdir -p "$HOME/sky_workdir" "$HOME/.sky/sky_app"
              python3 -m venv --without-pip --system-site-packages "$HOME/skypilot-runtime"
              /opt/sky-cpu/start-worker <<'SKYPILOT_SSH_KEY_EOF'
              skypilot:ssh_public_key_content
              SKYPILOT_SSH_KEY_EOF
              {{ ray_head_start_command | replace(rootless_prlimit, 'true;') }}
              touch /tmp/sky-cpu-ready
              exec /usr/sbin/sshd -D -e -f /opt/sky-cpu/sshd_config

"""
SETUP = """setup_commands:
  - |
    set -eu
    for i in $(seq 1 90); do
      if [ -f /tmp/sky-cpu-ready ]; then break; fi
      sleep 1
    done
    test -f /tmp/sky-cpu-ready
    python3 -c 'from importlib.metadata import version; assert version("skypilot") == "0.14.3"'
    ray status --address=127.0.0.1:6380 >/dev/null

"""


def install_file(target, expected_hash, transform, check):
    backup = target.with_name(target.name + ".stock-0.14.3")
    source = (backup if backup.exists() else target).read_bytes()
    if hashlib.sha256(source).hexdigest() != expected_hash:
        raise ValueError(f"Unqualified SkyPilot 0.14.3 source: {target.name}")
    adapted = transform(source.decode()).encode()
    if target.read_bytes() == adapted:
        return
    if target.read_bytes() != source:
        raise ValueError(f"{target.name} has unrecognized changes. Recreate the dedicated virtualenv from the lock.")
    if check:
        raise ValueError(f"{target.name} still has the stock implementation. Run the installer without --check.")
    if not backup.exists():
        with backup.open("xb") as f:
            f.write(source)
    with NamedTemporaryFile(dir=target.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(adapted)
            os.fchmod(f.fileno(), target.stat().st_mode & 0o777)
            f.flush()
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)


def adapt_pod(text, prlimit):
    start = text.index('          command: ["/bin/bash", "-c", "--"]')
    end = text.index(
        "          {% if not k8s_host_network %}\n          # Omitted under hostNetwork:",
        start,
    )
    text = text[:start] + POD.replace("rootless_prlimit", repr(prlimit)) + text[end:]
    start = text.index("\nsetup_commands:\n") + 1
    end = text.index("# Format: `REMOTE_PATH : LOCAL_PATH`", start)
    return text[:start] + SETUP + text[end:]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "venv",
        type=Path,
        help="Dedicated production or disposable qualification virtualenv",
    )
    p.add_argument(
        "--check", action="store_true", help="Require the matching installed adaptation"
    )
    a = p.parse_args()
    venv = a.venv.resolve(strict=True)
    if not (venv / "pyvenv.cfg").is_file():
        p.error("Expected a dedicated virtualenv")
    code = """from importlib.metadata import distribution
from sky.provision.instance_setup import RAY_PRLIMIT
import json
d = distribution('skypilot')
assert d.version == '0.14.3'
print(json.dumps([str(d.locate_file('sky/templates/kubernetes-ray.yml.j2')), RAY_PRLIMIT]))"""
    filename, prlimit = json.loads(
        subprocess.check_output([venv / "bin/python", "-I", "-c", code], text=True)
    )
    target = Path(filename).resolve(strict=True)
    if not target.is_relative_to(venv):
        p.error("Template resolves outside the requested virtualenv")
    runner = (target.parents[1] / "utils/command_runner.py").resolve(strict=True)
    helper = runner.parent / "kubernetes/rsync_helper.sh"
    if not runner.is_relative_to(venv) or not os.access(helper, os.X_OK):
        p.error("The dedicated environment requires an executable rsync helper")
    install_file(target, STOCK, lambda text: adapt_pod(text, prlimit), a.check)
    # The packaged helper already has its execute bit. The API can keep its
    # installation read-only while using the same kubectl rsync transport.
    install_file(
        runner,
        "042e3379bfc6b3c7e1f546918cecacb9392d688f6eba15280ab5794ad7f4ecf2",
        lambda text: text.replace(
            "f'chmod +x {helper_path} && '", "f'test -x {helper_path} && '"
        ),
        a.check,
    )
    print("Verified the rootless CPU server components")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error)) from None
