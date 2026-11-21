"""Run a single stage of the Lima forecast comparison inside its scoped pod."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import shutil
import time
from contextlib import ExitStack, contextmanager
from tempfile import NamedTemporaryFile, TemporaryDirectory
from pathlib import Path


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def bundle_manifest(manifest, digest, phase, head, *, paired=False):
    if manifest.is_symlink() or sha256(manifest) != digest:
        raise ValueError("Phase manifest checksum mismatch")
    spec = json.loads(manifest.read_text())
    if (spec["phase"], spec["head"]) != (phase, head):
        raise ValueError("Bundle belongs to another phase or model")
    cohort = spec.get("schema") == "forecast-direct-cohort-v1"
    if cohort != paired:
        raise ValueError("Direct cohort inputs require a pinned fit and inner-validation pair")
    if paired and (
        head != "direct" or phase not in {"fit", "inner_validation"} or spec.get("unit") != "ug/m3"
        or any(not re.fullmatch("[0-9a-f]{64}", str(spec.get(k, ""))) for k in
               ("protocol_sha256", "source_manifest_sha256", "weights_manifest_sha256", "ids_sha256"))
        or type(spec.get("windows")) is not int or spec["windows"] < 1
    ):
        raise ValueError("Incomplete direct cohort identity")
    return spec


def bundle(root, digest, phase, head, *, paired=False):
    spec = bundle_manifest(root / "manifest.json", digest, phase, head, paired=paired)
    expected = {"data.npz", "thresholds.json"} if head == "episode" or paired else {"data.npz"}
    if "point_control_of" in spec:
        expected.update(("quantile-fit.json", "quantile-checkpoint.json"))
    if set(spec["files"]) != expected or {
        p.name for p in root.iterdir()
    } != expected | {"manifest.json"}:
        raise ValueError("Unexpected phase inputs")
    for name in expected:
        path = root / name
        if path.is_symlink() or sha256(path) != spec["files"][name]:
            raise ValueError("Corrupt phase input")
    if "point_control_of" in spec:
        paired_recipe(root, spec)
    return spec


def paired_recipe(root, spec):
    if (
        spec["phase"],
        spec["head"],
        spec.get("schema"),
        spec.get("block"),
        spec.get("unit"),
    ) != ("fit", "direct", "forecast-input-v1", "train", "ug/m3"):
        raise ValueError("Expected a matched train-only point fit")
    if spec["point_control_of"] != spec["files"][
        "quantile-fit.json"
    ] or not re.fullmatch("[a-f0-9]{64}", spec.get("split_sha256", "")):
        raise ValueError("Quantile source or split digest mismatch")
    original = json.loads((root / "quantile-fit.json").read_text())
    if (
        original.get("head") != "quantile"
        or original.get("files") != {"data.npz": spec["files"]["data.npz"]}
        or any(
            original.get(k) != spec[k]
            for k in (
                "phase",
                "block",
                "schema",
                "unit",
                "weights_manifest_sha256",
                "split_sha256",
            )
        )
    ):
        raise ValueError(
            "Direct and quantile bundles describe different training inputs"
        )
    m = json.loads((root / "quantile-checkpoint.json").read_text())
    identity = m["identity"]
    expected = {
        "head": "quantile",
        "data": spec["files"]["data.npz"],
        "weights": spec["weights_manifest_sha256"],
        "input_manifest_sha256": spec["point_control_of"],
        "world": 1,
        "mode": "single",
        "precision": "fp32",
        "lora": True,
    }
    if (
        any(identity.get(k) != v for k, v in expected.items())
        or m["step"] != identity.get("schedule")
        or identity.get("task_schema", {}).get("lora_rank") != 4
        or identity.get("task_schema", {}).get("lora_alpha") != 8
    ):
        raise ValueError(
            "Source checkpoint does not identify the completed quantile recipe"
        )
    return {k: identity[k] for k in ("seed", "batch", "schedule")}


def prepare_point_control(source, source_digest, checkpoint, checkpoint_digest, output):
    original = bundle(source, source_digest, "fit", "quantile")
    if (
        original.get("schema") != "forecast-input-v1"
        or original.get("block") != "train"
        or original.get("unit") != "ug/m3"
        or not re.fullmatch("[0-9a-f]{64}", original.get("split_sha256", ""))
    ):
        raise ValueError("Expected a verified quantile training bundle")
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp) / "fit"
        stage.mkdir()
        shutil.copyfile(source / "data.npz", stage / "data.npz")
        shutil.copyfile(source / "manifest.json", stage / "quantile-fit.json")
        shutil.copyfile(checkpoint, stage / "quantile-checkpoint.json")
        files = {
            **original["files"],
            "quantile-fit.json": source_digest,
            "quantile-checkpoint.json": checkpoint_digest,
        }
        spec = {
            **original,
            "head": "direct",
            "point_control_of": source_digest,
            "files": files,
        }
        (stage / "manifest.json").write_text(json.dumps(spec, sort_keys=True) + "\n")
        digest = sha256(stage / "manifest.json")
        bundle(stage, digest, "fit", "direct")
        stage.rename(output)
    return digest


def bind_file(path, value):
    payload = json.dumps(value, sort_keys=True)
    with NamedTemporaryFile(dir=path.parent, mode="w") as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        try:
            os.link(tmp.name, path)
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            if path.read_text() != payload:
                raise ValueError(f"Published {path.name} belongs to another stage")


@contextmanager
def stage_attempt(root, key, identity, artifact, verify, *, with_publisher=False):
    with (root / ".episode-worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        intent = root / f".{key}.intent.json"
        if artifact.exists() and not intent.exists():
            raise FileExistsError(
                f"Existing artifact lacks its original intent: {artifact}"
            )
        bind_file(intent, identity)
        receipt = root / f".{key}.receipt.json"
        reuse = artifact.exists()
        if receipt.exists() and not reuse:
            raise ValueError("Published artifact missing")
        published = False

        def publish_verified():
            nonlocal published
            if not published:
                verify()
                bind_file(receipt, sha256(artifact))
                published = True

        if reuse:
            publish_verified()
        yield (reuse, publish_verified) if with_publisher else reuse
        publish_verified()


def verify_base(weights, expected):
    sums = weights.parent / "SHA256SUMS"
    if weights.is_symlink() or sums.is_symlink() or sha256(sums) != expected:
        raise ValueError("Base weights manifest mismatch")
    listed = {
        line.split(maxsplit=1)[1].removeprefix("*")
        for line in sums.read_text().splitlines()
    }
    actual = {
        str(path.relative_to(weights.parent))
        for path in weights.rglob("*")
        if path.is_file()
    }
    if (
        not actual
        or actual != listed
        or any(path.is_symlink() for path in weights.rglob("*"))
    ):
        raise ValueError("Unlisted or missing base weights")
    subprocess.run(
        ["sha256sum", "--check", "--strict", "--status", sums.name],
        cwd=weights.parent,
        check=True,
    )


def verify_fields(names, phase, head):
    fields = {"window_id", "history"}
    if head == "episode":
        fields.update(("station_key", "threshold_ug_m3", "thresholds_sha256"))
    if phase == "fit":
        fields.add("event" if head == "episode" else "targets")
    if set(names) != fields:
        raise ValueError("Fields outside phase schema")


def training_metrics(event):
    metrics = {
        k: float(v)
        for k, v in event.items()
        if k != "step" and isinstance(v, (float, int))
    }
    for name in ("mae", "coverage", "width", "crossed"):
        for h, v in enumerate(event.get(name + "_by_horizon", []), 1):
            metrics[f"{name}_h{h}"] = float(v)
    return metrics


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("fit", "infer"))
    parser.add_argument("head", choices=("direct", "quantile", "episode"))
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--step", type=int, required=True)
    parser.add_argument("--schedule", type=int)
    parser.add_argument("--seed", type=int, default=27)
    parser.add_argument("--direct-objective", choices=("normalized-mse", "physical-mae"))
    parser.add_argument("--validation-sha256")
    parser.add_argument("--batch", type=int, default=16)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--save-seconds", type=int, default=900)
    parser.add_argument("--graceful-stop", action="store_true")
    parser.add_argument("--stop-after-seconds", type=int)
    args = parser.parse_args()
    if args.stop_after_seconds is not None and (not args.graceful_stop or args.stop_after_seconds < 1):
        parser.error("A soft deadline requires cooperative fit shutdown")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", args.run_id) or args.step < 1:
        parser.error("Invalid run ID or step")
    if args.direct_objective or args.validation_sha256:
        if (not args.direct_objective or not re.fullmatch("[0-9a-f]{64}", args.validation_sha256 or "")
                or args.validation_sha256 == args.bundle_sha256 or (args.phase, args.head) != ("fit", "direct")
                or args.batch != 16 or args.seed not in (27, 28) or args.step != args.schedule):
            parser.error("Invalid paired direct fit")
    control = None
    if args.graceful_stop:
        if args.preflight or args.phase != "fit":
            parser.error("Cooperative stop requires a fit worker")
        from episode_stop import acknowledge, arm, channel

        control = channel(args.run_id, args.step, args.attempt)
        requested = arm(control, args.stop_after_seconds)

    def stop_before_launch():
        if control and requested.stat().st_size:
            acknowledge(control, "stopped-before-launch")
            raise SystemExit(75)

    import numpy as np

    root, weights = Path("/bundle"), Path("/base/weights")
    if args.direct_objective:
        from opentslm.forecast_inputs import direct_cohorts

        spec = bundle_manifest(root / "manifest.json", args.bundle_sha256, "fit", "direct", paired=True)
        validation_root = Path("/validation")
        fit_rows, validation_rows = direct_cohorts(
            root / "manifest.json", args.bundle_sha256, validation_root / "manifest.json",
            args.validation_sha256, spec["weights_manifest_sha256"],
        )
        if (len(fit_rows["window_id"]) + args.batch - 1) // args.batch != args.schedule:
            raise ValueError("Fit cohort does not match the scheduled epoch")
        del fit_rows, validation_rows
    else:
        spec = bundle(root, args.bundle_sha256, args.phase, args.head)
    bundle_verified = time.monotonic()
    verify_base(weights, spec["weights_manifest_sha256"])
    weights_verified = time.monotonic()
    matched = "point_control_of" in spec
    if matched:
        if paired_recipe(root, spec) != {
            k: getattr(args, k) for k in ("seed", "batch", "schedule")
        }:
            raise ValueError(
                "Seed, batch, or schedule differs from the source quantile fit"
            )
        if not args.preflight or Path("/opt/conda/bin/python").exists():
            options = subprocess.check_output(
                ["/opt/conda/bin/python", "/opt/forecast/train_qwen_forecast.py", "--help"],
                text=True,
                env={**os.environ, "PYTHONPATH": "/opt/forecast/src"},
            )
            if "--matched-point" not in options:
                raise ValueError(
                    "Rebuild with an OpenTSLM revision supporting matched point controls"
                )

    data = root / "data.npz"
    if not args.direct_objective:
        with np.load(data, allow_pickle=False) as archive:
            verify_fields(archive.files, args.phase, args.head)
    if matched or args.head in ("quantile", "episode"):
        from opentslm.forecast_inputs import verify_candidate_input

        verify_candidate_input(
            data,
            root / "manifest.json",
            args.bundle_sha256,
            args.phase,
            args.head,
            spec.get("thresholds_sha256"),
        )
    if args.phase == "infer":
        checkpoint = Path("/checkpoint") / f"step-{args.step:08d}"
        if sha256(checkpoint / "manifest.json") != spec["checkpoint_sha256"]:
            raise ValueError("Selected checkpoint digest differs")
    print(json.dumps({"event": "verified_inputs", "run": args.run_id, "attempt": args.attempt,
                      "startup_and_bundle_seconds": bundle_verified - started,
                      "weights_seconds": weights_verified - bundle_verified,
                      "elapsed_seconds": time.monotonic() - started}), file=sys.stderr, flush=True)
    stop_before_launch()
    if args.preflight:
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "bundle_sha256": args.bundle_sha256,
                    **({"validation_sha256": args.validation_sha256, "objective": args.direct_objective} if args.direct_objective else {}),
                    "verified": True,
                }
            )
        )
        return
    import mlflow

    common = [
        "--head",
        args.head,
        "--weights",
        str(weights),
        "--data",
        str(data),
        "--batch",
        str(args.batch),
    ]
    if matched or args.head in ("quantile", "episode") or args.direct_objective:
        common += [
            "--input-manifest",
            str(root / "manifest.json"),
            "--input-manifest-sha256",
            args.bundle_sha256,
        ]
    if args.head == "episode":
        common += ["--thresholds-sha256", spec["thresholds_sha256"]]
    step_name = f"step-{args.step:08d}"
    if args.phase == "fit":
        if args.schedule is None or not args.step <= args.schedule:
            parser.error("Specify a fixed schedule covering the requested step")
        command = [
            "/opt/conda/bin/torchrun",
            "--standalone",
            "--nproc-per-node=1",
            "/opt/forecast/train_qwen_forecast.py",
            "--mode",
            "single",
            "--lora",
            *([] if args.direct_objective else ["--validate-at", "final"]),
            "--run-dir",
            "/state",
            "--seed",
            str(args.seed),
            "--stop-step",
            str(args.step),
            "--save-seconds",
            str(args.save_seconds),
            "--schedule-steps",
            str(args.schedule),
            *common,
        ]
        if args.direct_objective:
            command.extend(["--direct-objective", args.direct_objective, "--logical-run", args.run_id,
                            "--validation", str(validation_root / "data.npz"), "--validation-manifest",
                            str(validation_root / "manifest.json"), "--validation-manifest-sha256", args.validation_sha256])
        if matched:
            command.append("--matched-point")
        if args.resume:
            command.append("--resume")
        artifact = Path("/state") / ("direct-result.json" if args.direct_objective else step_name + "/manifest.json")
    else:
        checkpoint = Path("/checkpoint") / step_name
        artifact = Path("/output") / f"predictions-{step_name}.npz"
        command = [
            "/opt/conda/bin/python",
            "-m",
            "opentslm.forecast_inference",
            "--checkpoint",
            str(checkpoint),
            "--require-history-only",
            "--output",
            str(artifact),
            *common,
        ]
    work = Path("/state") if args.phase == "fit" else artifact.parent

    def verify_result(generation=None):
        if args.phase == "fit":
            from opentslm.forecast_checkpoint import files, read_manifest

            result = None
            directory = generation or artifact.parent
            if args.direct_objective and generation is None:
                result = json.loads(artifact.read_bytes())
                if (type(result["step"]) is not int or not 0 < result["step"] <= args.step or
                        type(result["best_step"]) is not int or not 0 < result["best_step"] <= result["step"] or
                        result["outcome"] != ("completed" if result["step"] == args.step else "early-stopped")):
                    raise ValueError("Unexpected terminal direct fit result")
                directory = work / f"step-{result['step']:08d}"
                selected = work / f"step-{result['best_step']:08d}"
                best = read_manifest(selected)
                if (not best or best["identity"] != result["identity"] or best["files"] != files(selected) or
                        sha256(selected / "manifest.json") != result["selected_manifest_sha256"] or
                        sha256(directory / "manifest.json") != result["latest_manifest_sha256"] or
                        (work / "best").read_text().strip() != selected.name):
                    raise ValueError("Terminal direct checkpoint failed verification")
                last = json.loads((directory / "training.pt.json").read_bytes())["evaluations"][-1]
                if any(result.get(k) != v for k, v in last.items()):
                    raise ValueError("Terminal result differs from durable evaluation")

            m = read_manifest(directory)
            if (not m or not 0 < m["step"] <= args.step or m["files"] != files(directory)
                    or generation is None and result is None and m["step"] != args.step):
                raise ValueError("Incomplete checkpoint")
            identity = m["identity"]
            expected = {
                "data": spec["files"]["data.npz"],
                "head": args.head,
                "seed": args.seed,
                "schedule": args.schedule,
                "weights": spec["weights_manifest_sha256"],
            }
            if args.direct_objective:
                expected.update(logical_run=args.run_id, direct_objective=args.direct_objective,
                                input_manifest_sha256=args.bundle_sha256, validation_manifest_sha256=args.validation_sha256)
                if result is not None and result["identity"] != identity:
                    raise ValueError("Terminal result carries another run identity")
            if matched:
                expected.update(matched_point="complete_epoch_v1", input_manifest_sha256=args.bundle_sha256)
            if any(identity.get(k) != v for k, v in expected.items()):
                raise ValueError("Published checkpoint does not match this fit")
        else:
            with (
                np.load(data, allow_pickle=False) as inputs,
                np.load(artifact, allow_pickle=False) as result,
            ):
                if result["checkpoint_sha256"].item() != spec[
                    "checkpoint_sha256"
                ] or not np.array_equal(result["window_id"], inputs["window_id"]):
                    raise ValueError("Published prediction provenance mismatch")
                if (
                    args.head != "direct"
                    and result["input_manifest_sha256"].item() != args.bundle_sha256
                ):
                    raise ValueError("Published input manifest mismatch")
                key = {
                    "direct": "predicted",
                    "episode": "episode_logit",
                    "quantile": "quantiles",
                }[args.head]
                shape = {"direct": (6,), "episode": (), "quantile": (6, 3)}[args.head]
                if (
                    result[key].shape != (len(inputs["window_id"]), *shape)
                    or not np.isfinite(result[key]).all()
                ):
                    raise ValueError("Invalid published predictions")

    identity = {
        k: v
        for k, v in vars(args).items()
        if (v is not None or k not in ("direct_objective", "validation_sha256"))
        and k not in ("attempt", "resume", "preflight", "save_seconds", "graceful_stop", "stop_after_seconds")
    }
    identity["image"] = os.environ["EPISODE_IMAGE"]
    experiment = mlflow.set_experiment("airaware-pm25")
    with ExitStack() as stack:
        reused, publish_verified = stack.enter_context(
            stage_attempt(work, f"{args.phase}-{step_name}", identity, artifact, verify_result, with_publisher=True)
        )
        tracking_id = None
        tags = {}
        existing = []
        if args.direct_objective:
            key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            tags["direct_fit_id"] = key
            client = mlflow.MlflowClient()
            existing = client.search_runs([experiment.experiment_id], f"tags.direct_fit_id = '{key}'", max_results=2)
            if len(existing) > 1:
                raise ValueError("Multiple tracking runs already own this paired fit")
            if existing:
                prior = existing[0]
                tracking_id = prior.info.run_id
                if reused and prior.info.status == "FINISHED" and prior.data.tags.get("artifact_sha256") == sha256(artifact):
                    if control:
                        selected_step = json.loads(artifact.read_bytes())["best_step"]
                        acknowledge(control, "complete", work / f"step-{selected_step:08d}")
                    return
        run = stack.enter_context(mlflow.start_run(run_id=tracking_id, run_name=f"{args.run_id}-{args.phase}", tags=tags))
        seen_steps = {}

        def log_metrics(metrics, step):
            if existing:
                for name in metrics:
                    if name not in seen_steps:
                        seen_steps[name] = {entry.step for entry in client.get_metric_history(run.info.run_id, name)}
                metrics = {k: v for k, v in metrics.items() if step not in seen_steps[k]}
            if metrics:
                mlflow.log_metrics(metrics, step=step)

        mlflow.log_params(
            {
                **(identity if args.direct_objective else vars(args)),
                "infra_sha": os.environ["INFRA_SOURCE_REVISION"],
                "trainer_sha": os.environ["FORECAST_SOURCE_REVISION"],
                "airaware_sha": os.environ["AIRAWARE_SOURCE_REVISION"],
                "image": os.environ["EPISODE_IMAGE"],
                "lora_rank": 4,
                "lora_alpha": 8,
                "weights_manifest_sha256": spec["weights_manifest_sha256"],
            }
        )
        mlflow.set_tags(
            {
                k: spec[k]
                for k in (
                    "thresholds_sha256",
                    "checkpoint_sha256",
                    "split_sha256",
                    "fixture_sha256",
                    "point_control_of",
                    "schema",
                    "unit",
                )
                if k in spec
            }
        )
        mlflow.log_artifact(str(root / "manifest.json"), artifact_path="inputs")
        mlflow.set_tags(
            {"schema": spec.get("schema", "legacy"), "unit": spec.get("unit", "ug/m3")}
        )
        env = {**os.environ, "PYTHONPATH": "/opt/forecast/src"}
        if control:
            env["EPISODE_STOP_FILE"] = str(requested)
        else:
            env.pop("EPISODE_STOP_FILE", None)
        mlflow.set_tag("artifact_reused", str(reused))
        if not reused:
            stop_before_launch()
            interrupted = None
            with subprocess.Popen(
                command, stdout=subprocess.PIPE, text=True, env=env
            ) as proc:
                for line in proc.stdout:
                    print(line, end="", flush=True)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if control and isinstance(event, dict) and event.get("stopped_before_update") is True:
                        interrupted = 0
                    if control and isinstance(event, dict) and "interrupted_at_step" in event:
                        interrupted = int(event["interrupted_at_step"])
                    if control and requested.stat().st_size:
                        continue
                    if isinstance(event, dict) and "peak_bytes_per_rank" in event:
                        log_metrics({"peak_gpu_allocated_bytes": max(v[0] for v in event["peak_bytes_per_rank"])}, event["step"])
                    if isinstance(event, dict):
                        metrics = training_metrics(event)
                        if metrics:
                            log_metrics(metrics, event.get("step", 0))
                if proc.wait():
                    raise subprocess.CalledProcessError(proc.returncode, command)
            if interrupted is not None:
                if interrupted == 0 and requested.stat().st_size:
                    acknowledge(control, "stopped-before-update")
                    raise SystemExit(75)
                if not requested.stat().st_size or not 0 < interrupted < args.step:
                    raise ValueError("Unexpected trainer interruption")
                generation = work / f"step-{interrupted:08d}"
                verify_result(generation)
                acknowledge(control, "interrupted", generation)
                raise SystemExit(75)
        publish_verified()
        selected_dir = artifact.parent
        if args.direct_objective:
            terminal = json.loads(artifact.read_bytes())
            selected_dir = work / f"step-{terminal['best_step']:08d}"
            mlflow.log_params({"actual_stop_step": terminal["step"], "selected_step": terminal["best_step"],
                               "selected_manifest_sha256": terminal["selected_manifest_sha256"]})
            mlflow.set_tag("fit_outcome", terminal["outcome"])
            terminal_dir = work / f"step-{terminal['step']:08d}"
            for name in ("manifest.json", "training.pt.json"):
                mlflow.log_artifact(str(terminal_dir / name), "evaluations")
            mlflow.log_artifact(str(validation_root / "manifest.json"), "inputs/inner_validation")
            mlflow.log_param("recipe_sha256", terminal["identity"]["recipe_sha256"])
        mlflow.log_metrics(
            {
                "duration_seconds": time.monotonic() - started,
                "filesystem_free_bytes": shutil.disk_usage(work).free,
                "inputs_filesystem_free_bytes": shutil.disk_usage(root).free,
            }
        )
        host_mem = Path("/sys/fs/cgroup/memory.peak")
        if host_mem.is_file() and host_mem.read_text().strip().isdigit():
            mlflow.log_metric("pod_peak_memory_bytes", int(host_mem.read_text()))
        mlflow.set_tag(
            "state_path",
            f"episodes/{args.run_id}/"
            + (
                "state/" + (selected_dir.name if args.direct_objective else step_name)
                if args.phase == "fit"
                else "output/" + artifact.name
            ),
        )
        mlflow.log_artifact(str(artifact))
        mlflow.set_tag("artifact_sha256", sha256(artifact))
    if control:
        acknowledge(control, "complete", selected_dir)


if __name__ == "__main__":
    main()
