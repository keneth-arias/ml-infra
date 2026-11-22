#!/usr/bin/env python3
"""Submit the fixed staging Job to managed Kubernetes with the signed preflight image."""

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from production_release import model_roles, release_images, require_image
from production_target import kubectl_client_for_identity


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    renderer = Path(__file__).with_name("stage-episode.py")
    if args in (["--help"], ["-h"]):
        print(__doc__, flush=True)
        subprocess.run([sys.executable, renderer, "--help"], check=True)
        return
    images = release_images()
    rendered = subprocess.run([sys.executable, renderer, *args], text=True, capture_output=True)
    if rendered.returncode:
        raise SystemExit("Invalid staging arguments. See --help for the required inputs.")
    job = json.loads(rendered.stdout)
    container = job["spec"]["template"]["spec"]["containers"][0]
    image = container["image"]
    _, role = model_roles(container["command"])
    require_image(images, role, image)

    # Staging writes the input claim under separate, operator-approved credentials.
    operator = os.environ["NEBIUS_STAGING_OPERATOR_IDENTITY"]
    if not operator or operator in ("system:anonymous", "system:serviceaccount:training:episode-submit"):
        raise ValueError("Select the approved staging operator identity")
    client = kubectl_client_for_identity("training", operator, direct=True)
    name = "stage-episode-" + uuid.uuid4().hex[:12]
    del job["metadata"]["generateName"]
    job["metadata"]["name"] = name
    require_image(release_images(), role, image)
    result = subprocess.run([*client, "--request-timeout=30s", "create", "-f", "-", "-o", "name"],
                            input=json.dumps(job), text=True, capture_output=True)
    if result.returncode or result.stdout.strip() != "job.batch/" + name:
        # Admission diagnostics may contain the submitted Job and its source details.
        raise SystemExit(f"Staging creation was not confirmed. Inspect training Job {name} before retrying.")
    print("job.batch/" + name)


if __name__ == "__main__":
    main()
