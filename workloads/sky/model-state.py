# /// script
# requires-python = ">=3.11"
# dependencies = ["PyYAML==6.0.3"]
# ///
"""Render the paired training task with an independent retained model volume."""

import sys
from pathlib import Path

import yaml


def model_task(template, joint=False):
    task = yaml.safe_load(template.read_text())
    spec = task["config"]["kubernetes"]["pod_config"]["spec"]
    (mount,) = [m for m in spec["containers"][0]["volumeMounts"] if m["mountPath"] == "/checkpoints"]
    mount.update(name="model-state")
    mount.pop("subPath")
    spec["volumes"].append({
        "name": "model-state", "persistentVolumeClaim": {"claimName": "airaware-model-state"},
    })
    if joint:
        mounts = spec["containers"][0]["volumeMounts"]
        inputs = next(m for m in mounts if m["mountPath"] == "/inputs")
        inputs["subPath"] = "airaware/joint-inputs"
        mounts.append({"name": "storage", "mountPath": "/inputs/qwen", "subPath": "airaware/inputs/qwen", "readOnly": True})
        task["resources"]["image_id"] = (
            "docker:docker.io/ml-infra/airaware-joint@"
            "sha256:14eb3ed9b9ca00c75acbc56991d0f1882dc8efc5306ccf5fa90366673f40b3be"
        )
        task["envs"].update(INPUT_VERSION="lima-pm25-pm10-v1", LORA="1", BATCH="16", VALIDATE_AT="final")
    return task


if __name__ == "__main__":
    print(yaml.safe_dump(model_task(Path(sys.argv[1]), "joint" in sys.argv[2:]), sort_keys=False), end="")
