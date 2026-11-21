# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Check the actual SkyPilot backend's CPU image admission using server-side dry runs."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workloads/sky"))
from production_target import kubectl_client


def check(client, namespace, image, template):
    if not re.fullmatch(r"ghcr\.io/keneth-arias/ml-infra-sky-cpu@sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected the rootless CPU repository and an immutable index digest")
    spec = yaml.safe_load(template)["config"]["kubernetes"]["pod_config"]["spec"]
    spec.update(serviceAccountName="skypilot-service-account", restartPolicy="Never")
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"namespace": namespace, "name": "cpu-policy-" + uuid.uuid4().hex[:12]},
        "spec": spec,
    }
    cmd = [*client, "--request-timeout=15s", "create", "--dry-run=server", "-f", "-"]
    # Change only the image. A quota, transport or authorization failure is inconclusive.
    other_digit = "0" if image[-1] != "0" else "1"
    spec["containers"][0]["image"] = image[:-1] + other_digit
    refusal = subprocess.run(cmd, input=json.dumps(pod), text=True, capture_output=True)
    if refusal.returncode == 0 or "SkyPilot requires its separately qualified CPU image" not in refusal.stderr:
        raise RuntimeError(f"Expected a SkyPilot image-policy denial. {refusal.stderr.strip()}")
    spec["containers"][0]["image"] = image
    subprocess.run(cmd, input=json.dumps(pod), text=True, check=True)
    print(f"Backend admission verified for {image}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="Rootless CPU reference from the independently verified release")
    args = parser.parse_args()
    client = kubectl_client("sky-cpu", "sky-backend", direct=True)
    task = ROOT / "bootstrap/nebius/management/rootless/qualification-task.yaml"
    check(client, "sky-cpu", args.image, task.read_text())
