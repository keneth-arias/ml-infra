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
from tempfile import TemporaryDirectory
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workloads/sky"))
from production_release import release_images, require_image
from production_target import kubectl_client, require_sky


def check(client, namespace, image, template):
    if not re.fullmatch(r"ghcr\.io/keneth-arias/ml-infra-sky-cpu@sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected the rootless CPU repository and an immutable index digest")
    config = yaml.safe_load(template)["config"]["kubernetes"]
    spec = config["pod_config"]["spec"]
    # SkyPilot 0.14.3 resolves NONE to this account before rendering the Pod.
    if config.get("remote_identity") != "NONE" or {"serviceAccount", "serviceAccountName"} & spec.keys():
        raise ValueError("Use the qualified SkyPilot NONE identity without a task account override")
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", help="Expected rootless CPU reference from the signed release")
    parser.add_argument("--launch", action="store_true",
                        help="Launch the verified rootless task on the funded CPU worker after probing admission")
    args = parser.parse_args(argv)
    images = release_images()
    image = images["rootless-cpu"]
    if args.image is not None:
        require_image(images, "rootless-cpu", args.image)
    client = kubectl_client("sky-cpu", "sky-backend", direct=True)
    template = (ROOT / "bootstrap/nebius/management/rootless/qualification-task.yaml").read_text()
    check(client, "sky-cpu", image, template)
    if args.launch:
        require_sky("sky-cpu")
        task = yaml.safe_load(template)
        task["resources"]["image_id"] = "docker:" + image
        cluster = "cpu-rootless-" + uuid.uuid4().hex[:12]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "task.yaml"
            path.write_text(yaml.safe_dump(task))
            require_image(release_images(), "rootless-cpu", image)
            print(f"Launching the rootless pilot on {cluster}", flush=True)
            subprocess.run(["sky", "launch", "-y", "-c", cluster, str(path)], check=True)


if __name__ == "__main__":
    main()
