"""Render a one-GPU AirAware job for bm or a SkyPilot task with the same inputs."""

import argparse
import hashlib
import json
import re
import shlex
from pathlib import Path


def validate_staged_launch(
    path, release_sha, bundle_sha, run_id, head, image, seed, step, batch
):
    launch = json.loads(path.read_bytes())

    def checked(field, digest):
        raw = launch[field].encode()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Staged " + field + " digest differs")
        return json.loads(raw)

    release = checked("release_json", release_sha)
    seal_sha = release["components"]["policy.json"]
    seal = checked("seal_json", seal_sha)
    ck = checked("checkpoint_manifest_json", release["checkpoint_sha256"])
    spec = checked("input_manifest_json", bundle_sha)
    if (
        launch["status"] != "ready"
        or launch["run_id"] != run_id
        or release.get("purpose") != "retrospective_holdout_only"
        or ("inference_batch" in release and release["inference_batch"] != batch)
        or ck["step"] != step
        or seal["seed"] != seed
        or seal["head"] != head
        or release["image"] != seal["image"]
        or image != seal["image"]
        or spec["head"] != head
        or spec["seal_sha256"] != seal_sha
        or spec["checkpoint_sha256"] != release["checkpoint_sha256"]
        or spec["weights_manifest_sha256"] != release["base_manifest_sha256"]
    ):
        raise ValueError("Submission differs from the staged test release")


def pod_spec(phase, run_id, retained_release=None, step=None, *, weights_digest=None, bundle_digest=None, validation_digest=None):
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
    if weights_digest:
        mounts[0]["subPath"] = f"airaware/cache/bundles/{bundle_digest}"
        mounts[1]["subPath"] = f"airaware/cache/weights/{weights_digest}"
    if validation_digest:
        mounts.append({"name": "inputs", "mountPath": "/validation",
                       "subPath": f"airaware/cache/bundles/{validation_digest}", "readOnly": True})
    checkpoint_mount = {
        "name": "state",
        "mountPath": "/state" if phase == "fit" else "/checkpoint",
        "subPath": f"episodes/{run_id}/state",
        "readOnly": phase == "infer",
    }
    if retained_release:
        if (
            phase != "infer"
            or not re.fullmatch(r"[a-f0-9]{64}", retained_release)
            or not step
            or step < 1
        ):
            raise ValueError(
                "Expected a retained release digest and step for inference"
            )
        generation = f"step-{step:08d}"
        checkpoint_mount.update(
            mountPath=f"/checkpoint/{generation}",
            subPath=f"episodes/retained/{retained_release}/{generation}",
        )
    mounts.append(checkpoint_mount)
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
                "resources": {
                    "requests": {"memory": "12G"},
                    "limits": {},
                },
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
    p.add_argument("--target", choices=("dev-bm", "prod-nebius"), default="dev-bm")
    p.add_argument("--image", required=True)
    p.add_argument("--preflight-image", help="Digest-pinned CPU validator from the same source pair")
    p.add_argument("--gpu-limit-bytes", type=int, help="Explicit allocator ceiling for a qualified cloud run")
    p.add_argument("--cached-weights-sha256", help="Use verified digest-keyed PVC input directories")
    p.add_argument("--graceful-stop", action="store_true", help="Use a trainer with cooperative shutdown support")
    p.add_argument("--bundle-sha256", required=True)
    p.add_argument("--step", type=int, required=True)
    p.add_argument("--schedule", type=int)
    p.add_argument("--direct-objective", choices=("normalized-mse", "physical-mae"))
    p.add_argument("--flamingo-baselines", nargs=2, metavar=("FIRST_SHA", "REPEAT_SHA"))
    p.add_argument("--flamingo-binding-sha256", help="AirAware binding for the per-series Gemma forecast adapter")
    p.add_argument("--validation-sha256", help="Separately pinned direct inner-validation cohort")
    p.add_argument("--batch", type=int, default=16)
    p.add_argument("--seed", type=int, default=27)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--save-seconds", type=int, default=900)
    p.add_argument("--deadline-seconds", type=int, default=14400)
    p.add_argument("--resume-checkpoint", help="Verified STEP:SHA256 required for a funded resume")
    p.add_argument("--resume", action="store_true")
    p.add_argument(
        "--retained-release",
        help="Mount this verified release generation for inference",
    )
    p.add_argument(
        "--staged-launch", type=Path, help="JSON emitted by stage_holdout.py"
    )
    a = p.parse_args()
    if a.resume_checkpoint is not None and (not a.direct_objective or not a.resume or
            not re.fullmatch(r"[1-9][0-9]*:[a-f0-9]{64}", a.resume_checkpoint)):
        p.error("Checkpoint pin requires a paired resume and STEP:SHA256")
    if bool(a.retained_release) != bool(a.staged_launch):
        p.error(
            "Retained inference requires the release digest and staged launch record"
        )
    if a.staged_launch:
        validate_staged_launch(
            a.staged_launch,
            a.retained_release,
            a.bundle_sha256,
            a.run_id,
            a.head,
            a.image,
            a.seed,
            a.step,
            a.batch,
        )
    if not re.fullmatch("[a-z0-9][a-z0-9-]{0,40}[a-z0-9]", a.run_id):
        p.error("Invalid Kubernetes run name")
    if not re.fullmatch(
        r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", a.image
    ) or not re.fullmatch("[0-9a-f]{64}", a.bundle_sha256):
        p.error("Immutable image and bundle digests are required")
    if a.direct_objective or a.validation_sha256:
        if (not a.direct_objective or not re.fullmatch("[0-9a-f]{64}", a.validation_sha256 or "")
                or a.validation_sha256 == a.bundle_sha256 or not a.cached_weights_sha256
                or (a.phase, a.head) != ("fit", "direct") or a.batch != 16 or a.seed not in (27, 28)
                or a.step != a.schedule or a.schedule < 500):
            p.error("A matched direct fit requires both pinned cohorts, cached weights and a full batch-16 epoch")
    if bool(a.flamingo_binding_sha256) != bool(a.flamingo_baselines):
        p.error("Supply the Flamingo binding and both approved baseline receipts together")
    if a.flamingo_binding_sha256 is not None and (
        not all(re.fullmatch("[0-9a-f]{64}", d) for d in a.flamingo_baselines) or len(set(a.flamingo_baselines)) != 2
        or not re.fullmatch("[0-9a-f]{64}", a.flamingo_binding_sha256) or not a.direct_objective
        or a.target != "prod-nebius" or a.format == "sky" or not a.preflight_image
    ):
        p.error("Flamingo requires a pinned binding and paired native Nebius Job with its preflight image")
    if a.preflight_image and not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[0-9a-f]{64}", a.preflight_image):
        p.error("Immutable preflight image digest required")
    if a.cached_weights_sha256 and not re.fullmatch("[0-9a-f]{64}", a.cached_weights_sha256):
        p.error("Invalid cached weights digest")
    if a.gpu_limit_bytes is not None and (a.target != "prod-nebius" or not 0 < a.gpu_limit_bytes <= 96_000_000_000):
        p.error("Set a positive GPU limit within the 96 GB cloud profile")
    if a.graceful_stop and a.phase != "fit":
        p.error("Graceful stop is only available for fit")
    if (
        a.step < 1
        or a.attempt < 1
        or a.save_seconds < 0
        or a.deadline_seconds < 1
        or a.batch < 1
        or (a.phase == "fit" and (a.schedule is None or a.step > a.schedule))
    ):
        p.error("Invalid step, batch or fit schedule")
    if a.target == "prod-nebius" and a.format == "job" and a.deadline_seconds > 99 * 3600:
        p.error("Native GPU Job deadline exceeds the maximum 99-hour worker window")
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
    if a.direct_objective:
        cmd.extend(["--direct-objective", a.direct_objective, "--validation-sha256", a.validation_sha256])
    if a.flamingo_binding_sha256:
        cmd.extend(["--flamingo-binding-sha256", a.flamingo_binding_sha256, "--flamingo-baselines", *a.flamingo_baselines])
    if a.schedule is not None:
        cmd.extend(["--schedule", str(a.schedule)])
    if a.resume:
        cmd.append("--resume")
    if a.resume_checkpoint:
        cmd.extend(["--resume-checkpoint", a.resume_checkpoint])
    env = {
        "MLFLOW_TRACKING_URI": "http://mlflow.ml-platform.svc:5000",
        "EPISODE_IMAGE": a.image,
        "OMP_NUM_THREADS": "2",
        "HF_HUB_OFFLINE": "1",
    }
    if a.gpu_limit_bytes and a.format != "preflight":
        env["FORECAST_GPU_LIMIT_BYTES"] = str(a.gpu_limit_bytes)
    spec = pod_spec(a.phase, a.run_id, a.retained_release, a.step, weights_digest=a.cached_weights_sha256, bundle_digest=a.bundle_sha256, validation_digest=a.validation_sha256)
    spec["automountServiceAccountToken"] = False
    if a.graceful_stop and a.format != "preflight":
        cmd.append("--graceful-stop")
        spec["terminationGracePeriodSeconds"] = 90
        env.update(MLFLOW_HTTP_REQUEST_TIMEOUT="5", MLFLOW_HTTP_REQUEST_MAX_RETRIES="0")
    pod_uid = [{"name": "EPISODE_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}] if a.graceful_stop and a.format != "preflight" else []
    labels = {"airaware.training/run": a.run_id, "airaware.training/phase": a.phase,
              "airaware.training/attempt": str(a.attempt), "airaware.training/step": str(a.step)}
    cluster = None
    if a.graceful_stop and a.format == "sky":
        token = hashlib.sha256(f"{a.target}/{a.run_id}/{a.step}/{a.attempt}".encode()).hexdigest()[:16]
        cluster = "fit-" + a.run_id[:12] + "-" + token
        labels["airaware.training/sky-cluster"] = cluster
    production = a.target == "prod-nebius"
    tracking_env = []
    if production:
        del env["MLFLOW_TRACKING_URI"]
        spec.pop("runtimeClassName")
        spec["imagePullSecrets"] = [{"name": "ghcr-pull"}]
        spec["securityContext"] = {"fsGroup": 1000, "fsGroupChangePolicy": "OnRootMismatch"}
        # Publish the RWO PVC once. Bind input/checkpoint subpaths read-only
        # without making the underlying GPU state mount read-only as well.
        spec["volumes"] = [volume for volume in spec["volumes"] if volume["name"] == "inputs"]
        if a.format == "preflight":
            spec["volumes"][0]["persistentVolumeClaim"]["readOnly"] = True
        for mount in spec["containers"][0]["volumeMounts"]:
            mount["name"] = "inputs"
        if a.format != "sky":
            spec["serviceAccountName"] = "episode-worker"
        if a.format == "preflight":
            spec["nodeSelector"] = {"airaware.training/pool": "training-cpu"}
        else:
            spec["nodeSelector"] = {"airaware.training/pool": "training-gpu"}
            spec["tolerations"] = [
                {"key": "nvidia.com/gpu", "operator": "Equal", "value": "true", "effect": "NoSchedule"}
            ]
            from production_target import bind_tracking_client
            tracking_env = bind_tracking_client(spec)

    if a.format != "sky":
        c = spec["containers"][0]
        c.update(
            image=a.image,
            command=cmd,
            env=[{"name": k, "value": v} for k, v in env.items()] + tracking_env + pod_uid,
        )
        c["resources"]["requests"]["cpu"] = "2"
        if a.format == "preflight":
            c["resources"] = {"requests": {"cpu": "2", "memory": "1G"}}
            if a.preflight_image:
                c["image"] = a.preflight_image
                cmd[0] = "/usr/local/bin/python"
                c["resources"] = {"requests": {"cpu": "1", "memory": "256Mi"},
                                  "limits": {"cpu": "2", "memory": "512Mi"}}
            cmd.append("--preflight")
            spec.pop("runtimeClassName", None)
            c["volumeMounts"] = [m for m in c["volumeMounts"] if m.get("readOnly")]
            mount_names = {m["name"] for m in c["volumeMounts"]}
            spec["volumes"] = [v for v in spec["volumes"] if v["name"] in mount_names]
        else:
            c["resources"]["limits"]["nvidia.com/gpu"] = 1
            c["resources"]["requests"]["nvidia.com/gpu"] = 1
        spec.update(restartPolicy="Never", automountServiceAccountToken=False)
        deadline = a.deadline_seconds
        if production and a.format == "preflight":
            # The launcher waits ten minutes before abandoning the preflight.
            deadline = min(deadline, 600)
        template = {"metadata": {"labels": {"app": "airaware-episodes", **labels}}, "spec": spec}
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
                "activeDeadlineSeconds": deadline,
                "template": template,
            },
        }
    else:
        if tracking_env or pod_uid:
            spec["containers"][0]["env"] = tracking_env + pod_uid
        if production:
            if a.graceful_stop:
                cmd.extend(["--stop-after-seconds", str(a.deadline_seconds)])
                spec["activeDeadlineSeconds"] = a.deadline_seconds + 90
                cmd = ["timeout", "--signal=KILL", str(a.deadline_seconds + 90), *cmd]
            else:
                cmd = ["timeout", "--kill-after=30s", str(a.deadline_seconds), *cmd]
        result = {
            "name": cluster or "airaware-episodes",
            "num_nodes": 1,
            "resources": {
                "infra": "k8s/" + a.target,
                "accelerators": "rtxpro6000:1" if production else "rtx5070ti:1",
                "image_id": "docker:" + a.image,
                "cpus": 4,
                "memory": 12,
            },
            "config": {
                "kubernetes": {
                    "set_pod_resource_limits": False,"remote_identity": "NONE", "pod_config": {"metadata": {"labels": labels}, "spec": spec}}
            },
            "envs": env,
            "run": "export PYTHONPATH=/opt/forecast/src\nexec " + shlex.join(cmd),
        }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
