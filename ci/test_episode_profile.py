import json
import runpy
import subprocess
import unittest
from pathlib import Path

import yaml

profile = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py")
)


class ProfileTest(unittest.TestCase):
    def test_only_phase_inputs_are_visible(self):
        for phase in ("fit", "infer"):
            spec = profile["pod_spec"](phase, "demo")
            c = spec["containers"][0]
            mounts = {m["mountPath"]: m for m in c["volumeMounts"]}
            self.assertTrue(mounts["/base"]["readOnly"])
            self.assertEqual(
                mounts["/bundle"]["subPath"], "airaware/episodes/demo/" + phase
            )
            self.assertTrue(mounts["/bundle"]["readOnly"])
            self.assertNotIn("/opt/airaware", mounts)
            self.assertEqual(
                set(mounts) & {"/state", "/checkpoint"},
                {"/state"} if phase == "fit" else {"/checkpoint"},
            )
            if phase == "infer":
                self.assertTrue(mounts["/checkpoint"]["readOnly"])

    def test_retained_generation_is_read_only_and_never_used_for_fit(self):
        digest = "d" * 64
        pod = profile["pod_spec"]("infer", "station-test", digest, 14838)
        mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
        checkpoint = mounts["/checkpoint/step-00014838"]
        self.assertTrue(checkpoint["readOnly"])
        self.assertEqual(
            checkpoint["subPath"], f"episodes/retained/{digest}/step-00014838"
        )
        self.assertEqual(mounts["/output"]["subPath"], "episodes/station-test/output")
        self.assertNotIn("/checkpoint", mounts)
        for phase, release in (("fit", digest), ("infer", "../bad")):
            with self.assertRaises(ValueError):
                profile["pod_spec"](phase, "station-test", release, 14838)

    def test_preflight_only_mounts_inputs_and_never_requests_gpu(self):
        script = (
            Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py"
        )
        for phase in ("fit", "infer"):
            cmd = [
                "python3",
                str(script),
                "preflight",
                phase,
                "quantile",
                "demo",
                "--image",
                "example.com/test@sha256:" + "a" * 64,
                "--bundle-sha256",
                "b" * 64,
                "--step",
                "1",
            ]
            if phase == "fit":
                cmd.extend(["--schedule", "1"])
            job = json.loads(subprocess.check_output(cmd))
            pod = job["spec"]["template"]["spec"]
            c = pod["containers"][0]
            self.assertNotIn("runtimeClassName", pod)
            self.assertEqual(c["resources"]["requests"]["memory"], "1G")
            self.assertNotIn("memory", c["resources"].get("limits", {}))
            self.assertFalse(
                any("nvidia.com/gpu" in v for v in c["resources"].values())
            )
            self.assertEqual(
                {m["mountPath"] for m in c["volumeMounts"]},
                {"/base", "/bundle"}
                if phase == "fit"
                else {"/base", "/bundle", "/checkpoint"},
            )
            self.assertTrue(all(m["readOnly"] for m in c["volumeMounts"]))
            self.assertIn("--preflight", c["command"])

    def test_training_requests_memory_without_a_container_ceiling(self):
        workloads = Path(__file__).resolve().parents[1] / "workloads"
        for phase in ("fit", "infer"):
            for fmt in ("job", "sky"):
                args = [
                    "python3",
                    str(workloads / "sky/episode-profile.py"),
                    fmt,
                    phase,
                    "quantile",
                    "demo",
                    "--image",
                    "registry/test@sha256:" + "a" * 64,
                    "--bundle-sha256",
                    "b" * 64,
                    "--step",
                    "1",
                ]
                if phase == "fit":
                    args.extend(["--schedule", "1"])
                result = json.loads(subprocess.check_output(args))
                if fmt == "job":
                    spec = result["spec"]["template"]["spec"]
                    self.assertEqual(
                        spec["containers"][0]["resources"]["limits"],
                        {"nvidia.com/gpu": 1},
                    )
                else:
                    config = result["config"]["kubernetes"]
                    self.assertIs(config["set_pod_resource_limits"], False)
                    self.assertEqual(result["resources"]["accelerators"], "rtx5070ti:1")
                    spec = config["pod_config"]["spec"]
                resources = spec["containers"][0]["resources"]
                self.assertEqual(resources["requests"]["memory"], "12G")
                self.assertNotIn("memory", resources.get("limits", {}))

        for name in ("dual-gpu", "fsdp"):
            job = yaml.safe_load((workloads / "smoke" / f"{name}.yaml").read_text())
            resources = job["spec"]["template"]["spec"]["containers"][0]["resources"]
            self.assertIn("memory", resources["requests"])
            self.assertEqual(resources["limits"]["nvidia.com/gpu"], 2)
            self.assertNotIn("memory", resources["limits"])
        for name in ("airaware", "lora"):
            task = yaml.safe_load((workloads / "sky" / f"{name}.yaml").read_text())
            self.assertIs(
                task["config"]["kubernetes"]["set_pod_resource_limits"], False
            )
            self.assertGreater(task["resources"]["memory"], 0)

    def test_episode_image_does_not_stage_or_invoke_the_capped_worker(self):
        repo = Path(__file__).resolve().parents[1]
        for path in (
            "bootstrap/microk8s/build-episodes.sh",
            "runtime/Dockerfile.episodes",
            "runtime/episode_job.py",
        ):
            with self.subTest(path=path):
                self.assertNotIn("gpu_worker.py", (repo / path).read_text())

    def test_flamingo_selects_native_nebius_and_binds_its_funding_request(self):
        import sys
        repo = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo / "bootstrap/nebius"))
        from fit_attempt import describe

        script = repo / "workloads/sky/episode-profile.py"
        pin = "f" * 64
        args = ["fit", "direct", "flamingo-control", "--target", "prod-nebius",
                "--image", "trainer@sha256:" + "a"*64, "--preflight-image", "preflight@sha256:" + "b"*64,
                "--cached-weights-sha256", "e"*64, "--bundle-sha256", "c"*64,
                "--validation-sha256", "d"*64, "--step", "14084", "--schedule", "14084",
                "--direct-objective", "normalized-mse", "--graceful-stop", "--flamingo-binding-sha256", pin, "--flamingo-baselines", "1"*64, "2"*64]
        missing = subprocess.run([sys.executable, script, "job", *args], capture_output=True)
        self.assertNotEqual(missing.returncode, 0, "Language-only baseline approval accepted")
        args.extend(["--flamingo-parity-sha256", "3"*64])
        pre, job = (subprocess.check_output([sys.executable, script, mode, *args]) for mode in ("preflight", "job"))
        worker = json.loads(job)
        command = worker["spec"]["template"]["spec"]["containers"][0]["command"]
        self.assertEqual(command[command.index("--flamingo-parity-sha256") + 1], "3"*64)
        command[command.index("--flamingo-parity-sha256") + 1] = "4"*64
        with self.assertRaisesRegex(ValueError, "same approved"):
            describe(pre, json.dumps(worker).encode())
        self.assertEqual(describe(pre, job)["pair"]["flamingo_parity_sha256"], "3"*64)
        self.assertEqual(describe(pre, job)["pair"]["flamingo_binding"], pin)
        self.assertEqual(describe(pre, job)["pair"]["flamingo_baselines"], ["1"*64, "2"*64])
        self.assertNotEqual(subprocess.run([sys.executable, script, "job", *args[:-3]], capture_output=True).returncode, 0)
        for target, mode in (("dev-bm", "job"), ("prod-nebius", "sky")):
            with self.subTest(target=target, mode=mode):
                args[args.index("--target") + 1] = target
                self.assertNotEqual(subprocess.run([sys.executable, script, mode, *args], capture_output=True).returncode, 0)

    def test_paired_roles_have_separate_immutable_mounts_in_each_format(self):
        import json
        import subprocess
        import sys

        script = Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py"
        args = ["fit", "direct", "paired-fit", "--target", "prod-nebius", "--image", "test@sha256:"+"a"*64,
                "--cached-weights-sha256", "b"*64, "--bundle-sha256", "c"*64,
                "--validation-sha256", "d"*64, "--step", "14084", "--schedule", "14084",
                "--direct-objective", "physical-mae"]
        for mode in ("job", "preflight", "sky"):
            result = json.loads(subprocess.check_output([sys.executable, script, mode, *args]))
            pod = (result["config"]["kubernetes"]["pod_config"]["spec"] if mode == "sky"
                   else result["spec"]["template"]["spec"])
            mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
            for name, pin in (("/bundle", "c"*64), ("/validation", "d"*64)):
                self.assertTrue(mounts[name]["readOnly"])
                self.assertEqual(mounts[name]["subPath"], "airaware/cache/bundles/"+pin)
            self.assertEqual("/state" in mounts, mode != "preflight")
        args[args.index("d"*64)] = "c"*64
        self.assertNotEqual(subprocess.run([sys.executable, script, "job", *args], capture_output=True).returncode, 0)


class NebiusRenderTest(unittest.TestCase):
    def render(self, mode, *options):
        import json
        import subprocess

        script = Path(__file__).resolve().parents[1] / "workloads/sky/episode-profile.py"
        args = [str(script), mode, "fit", "direct", "cloud-run", "--image",
                "ghcr.io/example/trainer@sha256:" + "a" * 64, "--bundle-sha256",
                "b" * 64, "--schedule", "4", "--step", "4", "--target", "prod-nebius"]
        return json.loads(subprocess.check_output(["python3", *args, *options], stderr=subprocess.PIPE))

    def test_cloud_fit_and_cpu_preflight(self):
        for mode in ("job", "preflight"):
            pod = self.render(mode)["spec"]["template"]["spec"]
            self.assertEqual(pod["imagePullSecrets"], [{"name": "ghcr-pull"}])
            self.assertEqual(pod["serviceAccountName"], "episode-worker")
            self.assertFalse(pod["automountServiceAccountToken"])
            inputs = next(v for v in pod["volumes"] if v["name"] == "inputs")
            self.assertEqual(inputs["persistentVolumeClaim"]["claimName"], "airaware-checkpoints")
            self.assertEqual(bool(inputs["persistentVolumeClaim"].get("readOnly")), mode == "preflight")
            self.assertEqual(sum("persistentVolumeClaim" in v for v in pod["volumes"]), 1)
            mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
            self.assertTrue(mounts["/bundle"]["readOnly"])
            self.assertTrue(mounts["/base"]["readOnly"])
            self.assertEqual(pod["securityContext"]["fsGroup"], 1000)
            if mode == "job":
                self.assertEqual(mounts["/state"], {"name": "inputs", "mountPath": "/state",
                                                     "readOnly": False, "subPath": "episodes/cloud-run/state"})
            if mode == "preflight":
                self.assertNotIn("/state", mounts)
                self.assertNotIn("tolerations", pod)
                self.assertEqual(pod["nodeSelector"], {"airaware.training/pool": "training-cpu"})
            else:
                self.assertEqual(pod["nodeSelector"]["airaware.training/pool"], "training-gpu")
                self.assertEqual(pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"], 1)
                env = {item["name"]: item for item in pod["containers"][0]["env"]}
                self.assertIn("secretKeyRef", env["MLFLOW_TRACKING_TOKEN"]["valueFrom"])
                self.assertIn("configMapKeyRef", env["MLFLOW_TRACKING_URI"]["valueFrom"])

    def test_cloud_preflight_deadline_on_first_attempt_and_retry(self):
        for attempt, deadline, expected in (("1", "14400", 600), ("2", "120", 120)):
            job = self.render("preflight", "--attempt", attempt, "--deadline-seconds", deadline)
            self.assertEqual(job["spec"]["activeDeadlineSeconds"], expected)
            self.assertEqual(job["spec"]["template"]["spec"]["nodeSelector"], {"airaware.training/pool": "training-cpu"})
        self.assertEqual(self.render("job", "--deadline-seconds", "14400")["spec"]["activeDeadlineSeconds"], 14400)

    def test_native_gpu_job_deadline_fits_maximum_worker_window(self):
        maximum = 99 * 3600
        self.assertEqual(self.render("job")["spec"]["activeDeadlineSeconds"], 4 * 3600)
        self.assertEqual(self.render("job", "--deadline-seconds", str(maximum))["spec"]["activeDeadlineSeconds"], maximum)
        with self.assertRaises(subprocess.CalledProcessError):
            self.render("job", "--deadline-seconds", str(maximum + 1))
        self.assertEqual(self.render("preflight", "--deadline-seconds", str(maximum + 1))["spec"]["activeDeadlineSeconds"], 600)

    def test_cloud_sky_context_and_deadline(self):
        task = self.render("sky")
        self.assertEqual(task["resources"]["infra"], "k8s/prod-nebius")
        self.assertEqual(task["resources"]["accelerators"], "rtxpro6000:1")
        self.assertIn("timeout", task["run"])
