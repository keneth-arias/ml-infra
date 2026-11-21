import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workloads/sky"))
IMAGES = {
    "rootless-cpu": "ghcr.io/keneth-arias/ml-infra-sky-cpu@sha256:" + "a" * 64,
    "trainer": "ghcr.io/keneth-arias/ml-infra-qwen@sha256:" + "b" * 64,
    "preflight": "ghcr.io/keneth-arias/ml-infra-qwen@sha256:" + "c" * 64,
}


def entrypoint(script):
    return runpy.run_path(str(ROOT / "workloads" / script))["main"]


class ReleaseEntrypointTest(unittest.TestCase):
    def test_healthy_sky_backend_must_match_cluster_ca_and_namespace(self):
        from production_target import require_sky
        url = "https://pilot.example"
        env = {"NEBIUS_CPU_SKY_URL": url, "SKYPILOT_API_SERVER_ENDPOINT": url,
               "NEBIUS_TRAINING_CLUSTER_ID": "mk8scluster-test", "NEBIUS_CA_BUNDLE_SHA256": "a" * 64,
               "NETRC": "/fixture/reader", "REQUESTS_CA_BUNDLE": "/fixture/ca"}
        info = {"server": {"url": url, "status": "healthy", "version": "0.14.3"}, "client": {"version": "0.14.3"}}
        target = {"cluster_id": env["NEBIUS_TRAINING_CLUSTER_ID"], "ca_sha256": "a" * 64, "namespace": "sky-cpu"}
        for change in ({}, {"cluster_id": "other"}, {"ca_sha256": "b" * 64}, {"namespace": "dev"}):
            with self.subTest(change=change), patch.dict(os.environ, env), patch.object(
                subprocess, "check_output", side_effect=[json.dumps(info), json.dumps(target | change)]
            ) as call:
                if change:
                    with self.assertRaisesRegex(ValueError, "different Kubernetes target"):
                        require_sky("sky-cpu")
                else:
                    require_sky("sky-cpu")
                self.assertIn(url + "/.well-known/nebius-target", call.call_args.args[0])

    def test_probe_and_launch_reject_unapproved_cpu_image_before_contact(self):
        main = entrypoint("smoke/check-cpu-admission.py")
        with (
            patch.dict(main.__globals__, release_images=lambda: IMAGES, kubectl_client=Mock(), require_sky=Mock()),
            patch.object(subprocess, "run") as command,
        ):
            for options in ([], ["--launch"]):
                for image in (IMAGES["trainer"], IMAGES["rootless-cpu"][:-1] + "f"):
                    with self.subTest(options=options, image=image), self.assertRaises(ValueError):
                        main([image, *options])
            main.__globals__["kubectl_client"].assert_not_called()
            main.__globals__["require_sky"].assert_not_called()
            command.assert_not_called()

    def test_rootless_launch_uses_probed_image_and_scoped_endpoint(self):
        main = entrypoint("smoke/check-cpu-admission.py")
        backend, sky = Mock(return_value=["scoped-kubectl"]), Mock()
        actions = []

        def process(cmd, **kwargs):
            if cmd[0] == "sky":
                task = yaml.safe_load(Path(cmd[-1]).read_text())
                self.assertEqual(task["resources"]["infra"], "k8s/prod-nebius")
                self.assertEqual(task["resources"]["image_id"], "docker:" + IMAGES["rootless-cpu"])
                actions.append("launch")
                return subprocess.CompletedProcess(cmd, 0)
            self.assertEqual(cmd[0], "scoped-kubectl")
            self.assertIn("--dry-run=server", cmd)
            actions.append(json.loads(kwargs["input"])["spec"]["containers"][0]["image"])
            if len(actions) == 1:
                return subprocess.CompletedProcess(cmd, 1, stderr="SkyPilot requires its separately qualified CPU image")
            return subprocess.CompletedProcess(cmd, 0)

        for rotated in (False, True):
            actions.clear()
            backend.reset_mock()
            sky.reset_mock()
            active = {**IMAGES, "rootless-cpu": IMAGES["rootless-cpu"][:-1] + "f"} if rotated else IMAGES
            with (
                self.subTest(rotated=rotated),
                patch.dict(main.__globals__, release_images=Mock(side_effect=[IMAGES, active]),
                           kubectl_client=backend, require_sky=sky),
                patch.object(subprocess, "run", side_effect=process),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                if rotated:
                    with self.assertRaises(ValueError):
                        main(["--launch"])
                else:
                    main(["--launch"])
                backend.assert_called_once_with("sky-cpu", "sky-backend", direct=True)
                sky.assert_called_once_with("sky-cpu")
                self.assertNotEqual(actions[0], IMAGES["rootless-cpu"])
                self.assertEqual(actions[1:], [IMAGES["rootless-cpu"]] + ([] if rotated else ["launch"]))

    def test_training_access_rejects_swapped_release_before_contact(self):
        main = entrypoint("smoke/verify-training-access.py")
        with (
            patch.dict(main.__globals__, release_images=lambda: IMAGES, kubectl_client=Mock()),
            patch.object(sys, "argv", ["verify", "--trainer-image", IMAGES["preflight"],
                                       "--preflight-image", IMAGES["trainer"]]),
            patch.object(subprocess, "run") as command,
        ):
            with self.assertRaises(ValueError):
                main()
            main.__globals__["kubectl_client"].assert_not_called()
            command.assert_not_called()

    def test_held_training_access_checks_denial_without_an_approved_release(self):
        main = entrypoint("smoke/verify-training-access.py")
        release = Mock(side_effect=AssertionError("Held admission does not need a release"))
        backend = Mock(return_value=["scoped-kubectl"])

        def denied(cmd, **kwargs):
            if "can-i" in cmd:
                return subprocess.CompletedProcess(cmd, 1, stdout="no")
            self.assertIn("--dry-run=server", cmd)
            return subprocess.CompletedProcess(cmd, 1, stderr='cannot create resource "jobs"')

        with (
            patch.dict(main.__globals__, release_images=release, kubectl_client=backend, render_job=Mock(return_value={})),
            patch.object(sys, "argv", ["verify"]),
            patch.object(subprocess, "run", side_effect=denied),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            main()
        backend.assert_called_once_with("training", "episode-submit", direct=True)
        release.assert_not_called()

    def test_staging_verifies_operator_and_preflight_before_only_create(self):
        main = entrypoint("sky/submit-stage-episode.py")
        secret = "private-inventory"
        prefix = "https://storage.eu-south1.nebius.cloud/bucket/private-run/"
        options = ["--inventory-secret", secret, "--source-prefix", prefix, "--weights-sha256", "d" * 64,
                   "--bundle-sha256", "e" * 64, "--phase", "fit", "--head", "direct"]
        real_run = subprocess.run
        operator = "approved-staging-operator"
        with tempfile.NamedTemporaryFile(mode="w") as config:
            os.chmod(config.name, 0o600)
            for role, identity, api_fails in (
                ("trainer", operator, False), ("preflight", "other-principal", False),
                ("preflight", operator, True), ("preflight", operator, False),
            ):
                operations = []

                def process(cmd, **kwargs):
                    if cmd[0] == sys.executable:
                        return real_run(cmd, **kwargs)
                    self.assertEqual(cmd[:5], ["kubectl", "--kubeconfig", config.name, "--context", "prod-nebius"])
                    if "config" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout="training")
                    if "whoami" in cmd:
                        return subprocess.CompletedProcess(cmd, 0, stdout=identity)
                    self.assertEqual(cmd[5:7], ["-n", "training"])
                    self.assertIn("create", cmd)
                    job = json.loads(kwargs["input"])
                    operations.append(job)
                    self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["image"], IMAGES["preflight"])
                    name = job["metadata"]["name"]
                    self.assertRegex(name, r"^stage-episode-[0-9a-f]{12}$")
                    if api_fails:
                        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=prefix + secret)
                    return subprocess.CompletedProcess(cmd, 0, stdout="job.batch/" + name + "\n", stderr="")

                out, err = io.StringIO(), io.StringIO()
                with (
                    self.subTest(role=role, identity=identity, api_fails=api_fails),
                    patch.dict(main.__globals__, release_images=lambda: IMAGES),
                    patch.dict(os.environ, NEBIUS_SUBMIT_KUBECONFIG=config.name, NEBIUS_STAGING_OPERATOR_IDENTITY=operator,
                               NEBIUS_TRAINING_CLUSTER_ID="mk8scluster-test", NEBIUS_CA_BUNDLE_SHA256="a" * 64),
                    patch("production_target.pinned_kubeconfig_ca") as pin,
                    patch.object(subprocess, "run", side_effect=process),
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
                ):
                    if role == "preflight" and identity == operator and not api_fails:
                        main(["--image", IMAGES[role], *options])
                        self.assertEqual(out.getvalue().strip(), "job.batch/" + operations[0]["metadata"]["name"])
                    else:
                        with self.assertRaises((ValueError, SystemExit)) as error:
                            main(["--image", IMAGES[role], *options])
                        if api_fails:
                            self.assertIn(operations[0]["metadata"]["name"], str(error.exception))
                        self.assertNotIn(prefix, str(error.exception))
                        self.assertNotIn(secret, str(error.exception))
                    self.assertEqual(len(operations), int(role == "preflight" and identity == operator))
                    self.assertEqual(pin.call_count, int(role == "preflight"))
                    if pin.called:
                        self.assertTrue(pin.call_args.kwargs["direct"])
                    self.assertNotIn(prefix, out.getvalue() + err.getvalue())
                    self.assertNotIn(secret, out.getvalue() + err.getvalue())
