"""Render a one-GPU AirAware job for bm or a SkyPilot task with the same inputs."""

import argparse
import json
import re
import shlex


def pod_spec(phase, run_id):
    mounts = [
        {
            "name": "inputs",
            "mountPath": "/bundle",
            "subPath": f"airaware/episodes/{run_id}/{phase}",
            "readOnly": True,
        },
        {
            "name": "inputs",
            "mountPath": "/base",
            "subPath": "airaware/inputs/qwen",
            "readOnly": True,
        },
    ]
    mounts.append(
        {
            "name": "state",
            "mountPath": "/state" if phase == "fit" else "/checkpoint",
            "subPath": f"episodes/{run_id}/state",
            "readOnly": phase == "infer",
        }
    )
    if phase == "infer":
        mounts.append(
            {
                "name": "state",
                "mountPath": "/output",
                "subPath": f"episodes/{run_id}/output",
            }
        )
    return {
        "runtimeClassName": "nvidia",
        "containers": [
            {
                "name": "ray-node",
                "imagePullPolicy": "IfNotPresent",
                "volumeMounts": mounts,
                "resources": {"requests": {"memory": "12G"}},
            }
        ],
        "volumes": [
            {
                "name": "inputs",
                "persistentVolumeClaim": {"claimName": "airaware-checkpoints"},
            },
            {
                "name": "state",
                "persistentVolumeClaim": {"claimName": "airaware-model-state"},
            },
        ],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("format", choices=("job", "sky", "preflight"))
    p.add_argument("phase", choices=("fit", "infer"))
    p.add_argument("head", choices=("direct", "quantile", "episode"))
    p.add_argument("run_id")
    p.add_argument("--image", required=True)
    p.add_argument("--bundle-sha256", required=True)
    p.add_argument("--step", type=int, required=True)
    p.add_argument("--schedule", type=int)
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--seed", type=int, default=27)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--save-seconds", type=int, default=900)
    p.add_argument("--deadline-seconds", type=int, default=14400)
    p.add_argument("--resume", action="store_true")
    a = p.parse_args()
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,40}[a-z0-9]", a.run_id):
        p.error("Invalid Kubernetes run name")
    if not re.fullmatch(
        r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", a.image
    ) or not re.fullmatch("[0-9a-f]{64}", a.bundle_sha256):
        p.error("Immutable image and bundle digests are required")
    if (
        a.step < 1
        or a.attempt < 1
        or a.save_seconds < 0
        or a.deadline_seconds < 1
        or a.batch < 1
        or (a.phase == "fit" and (a.schedule is None or a.step > a.schedule))
    ):
        p.error("Invalid step, batch or fit schedule")
    if a.phase == "infer" and (a.resume or a.schedule is not None):
        p.error("Inference does not use a training schedule or resume")
    cmd = [
        "/opt/conda/bin/python",
        "/opt/ml-infra/episode_job.py",
        a.phase,
        a.head,
        "--run-id",
        a.run_id,
        "--bundle-sha256",
        a.bundle_sha256,
        "--step",
        str(a.step),
        "--seed",
        str(a.seed),
        "--batch",
        str(a.batch),
    ]
    cmd += ["--attempt", str(a.attempt), "--save-seconds", str(a.save_seconds)]
    if a.schedule is not None:
        cmd.extend(["--schedule", str(a.schedule)])
    if a.resume:
        cmd.append("--resume")
    env = {
        "MLFLOW_TRACKING_URI": "http://mlflow.ml-platform.svc:5000",
        "EPISODE_IMAGE": a.image,
        "OMP_NUM_THREADS": "2",
        "HF_HUB_OFFLINE": "1",
    }
    spec = pod_spec(a.phase, a.run_id)
    if a.format != "sky":
        c = spec["containers"][0]
        c.update(
            image=a.image,
            command=cmd,
            env=[{"name": k, "value": v} for k, v in env.items()],
        )
        c["resources"]["requests"]["cpu"] = "2"
        if a.format == "preflight":
            cmd.append("--preflight")
            c["resources"]["requests"]["memory"] = "1G"
            spec.pop("runtimeClassName")
            c["volumeMounts"] = [m for m in c["volumeMounts"] if m.get("readOnly")]
            mount_names = {m["name"] for m in c["volumeMounts"]}
            spec["volumes"] = [v for v in spec["volumes"] if v["name"] in mount_names]
        else:
            c["resources"]["limits"] = {"nvidia.com/gpu": 1}
            c["resources"]["requests"]["nvidia.com/gpu"] = 1
        spec.update(restartPolicy="Never", automountServiceAccountToken=False)
        template = {"metadata": {"labels": {"app": "airaware-episodes"}}, "spec": spec}
        result = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"{a.run_id}-{a.phase}-{a.step}-"
                + ("pre-" if a.format == "preflight" else "")
                + str(a.attempt),
                "namespace": "training",
            },
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": a.deadline_seconds,
                "template": template,
            },
        }
    else:
        result = {
            "name": "airaware-episodes",
            "num_nodes": 1,
            "resources": {
                "infra": "k8s/dev-bm",
                "accelerators": "rtx5070ti:1",
                "image_id": "docker:" + a.image,
                "cpus": 4,
                "memory": 12,
            },
            "config": {
                "kubernetes": {
                    "remote_identity": "NONE",
                    "set_pod_resource_limits": False,
                    "pod_config": {"spec": spec},
                }
            },
            "envs": env,
            "run": shlex.join(cmd),
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
