# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Render the paired training task with an independent retained model volume."""

import sys
from pathlib import Path

import yaml


def model_task(template):
    task = yaml.safe_load(template.read_text())
    spec = task["config"]["kubernetes"]["pod_config"]["spec"]
    (mount,) = [m for m in spec["containers"][0]["volumeMounts"] if m["mountPath"] == "/checkpoints"]
    mount.update(name="model-state")
    mount.pop("subPath")
    spec["volumes"].append({
        "name": "model-state", "persistentVolumeClaim": {"claimName": "airaware-model-state"},
    })
    return task


if __name__ == "__main__":
    print(yaml.safe_dump(model_task(Path(sys.argv[1])), sort_keys=False), end="")
