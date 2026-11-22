import copy
from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workloads/sky"))
import production_release as release


def candidate(repo, digit):
    return {"repository": repo, **{k: "sha256:" + digit * 64 for k in ("index", "linux_amd64", "config")}}


class ProductionReleaseTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.files = self.root / "current"
        self.files.mkdir()
        self.key = self.root / "throwaway.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-out", self.key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-out", self.root / "signer.pub"],
                       check=True, capture_output=True)
        self.release = {
            "schema": "nebius-image-release-v1",
            "rootless": {**candidate(release.ROOTLESS, "a"), "role": "rootless-cpu", "build_commit": "a" * 40},
            "paired": {"schema": "paired-forecast-candidate-v1", "build_commit": "b" * 40,
                       "forecast_commit": "c" * 40, "reader_commit": "d" * 40,
                       "images": {"trainer": candidate(release.PAIRED, "b"), "preflight": candidate(release.PAIRED, "c")}},
        }
        self.raw = (json.dumps(self.release) + "\n").encode()
        self.approval = {"schema": "nebius-image-deployment-v1", "deployment_commit": "e" * 40,
                         "cluster_id": "mk8scluster-test", "release_sha256": sha256(self.raw).hexdigest()}
        self.sign("release.json", self.raw)
        self.sign("deployment.json", json.dumps(self.approval).encode())

    def sign(self, name, raw):
        (self.files / name).write_bytes(raw)
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key, "-out", self.files / (name + ".sig")],
                       input=raw, check=True, capture_output=True)

    def verify(self):
        return release.verify_release(self.root, "mk8scluster-test", "e" * 40)

    def test_exact_signatures_and_deployment_binding(self):
        refs, fingerprint = self.verify()
        self.assertEqual(set(refs), set(release.ROLES))
        self.assertEqual(fingerprint, sha256((self.files / "deployment.json").read_bytes()).hexdigest())
        for field, wrong in (("cluster_id", "mk8scluster-other"), ("release_sha256", "0" * 64),
                             ("deployment_commit", "a" * 40)):
            with self.subTest(field=field):
                self.sign("deployment.json", json.dumps({**self.approval, field: wrong}).encode())
                with self.assertRaisesRegex(ValueError, "Deployment receipt"):
                    self.verify()

    def test_tampered_and_resigned_artifacts(self):
        for name in ("release.json", "deployment.json"):
            with self.subTest(name=name):
                path = self.files / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, "Untrusted signature"):
                    self.verify()
                path.write_bytes(original)
        # Even a separately valid release may not replace the deployed one.
        self.sign("release.json", self.raw + b" ")
        with self.assertRaisesRegex(ValueError, "Deployment receipt"):
            self.verify()

    def test_unapproved_signer(self):
        wrong = self.root / "another.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-out", wrong], check=True, capture_output=True)
        self.key = wrong
        self.sign("deployment.json", json.dumps(self.approval).encode())
        with self.assertRaisesRegex(ValueError, "Untrusted signature for deployment"):
            self.verify()
        self.sign("release.json", self.raw)
        with self.assertRaisesRegex(ValueError, "Untrusted signature"):
            self.verify()

    def test_native_policy_role_mapping_matches_signed_release(self):
        images, _ = self.verify()
        variables = [{"name": role + "Image", "expression": repr(images[role])}
                     for role in ("trainer", "preflight")]
        policy = {"kind": "ValidatingAdmissionPolicy", "metadata": {"name": "episode-submit-inputs"},
                  "spec": {"variables": [*variables, *[
                      {"name": name, "expression": expression}
                      for name, expression in release.QWEN_ONLY_VARIABLES.items()
                  ]], "validations": [release.QWEN_ONLY]}}
        release.check_native_policy(images, policy)
        variables[0]["expression"] = repr(images["preflight"])
        with self.assertRaisesRegex(ValueError, "approved trainer image"):
            release.check_native_policy(images, policy)
        variables[0]["expression"] = repr(images["trainer"])
        policy["spec"]["variables"].remove(variables[1])
        with self.assertRaisesRegex(ValueError, "approved preflight image"):
            release.check_native_policy(images, policy)

    def test_operator_digest_guard_with_signed_v1_v2_and_rollback(self):
        activation = runpy.run_path(str(ROOT / "bootstrap/nebius/management/native-activation.py"))
        held, _ = activation["held_policy_and_binding"]()
        pins = {v["name"]: v["expression"].strip("'").split("@")[-1] for v in held["spec"]["variables"]
                if v["name"] in release.ROLE_VARIABLES.values()}

        def pair(prefix):
            return copy.deepcopy(self.release["paired"]) | {"images": {
                role: candidate(release.PAIRED, "0") | {"index": pins[release.ROLE_VARIABLES[prefix + role]]}
                for role in ("trainer", "preflight")
            }}

        v1 = self.release | {"paired": pair("")}
        v2 = v1 | {"schema": "nebius-image-release-v2", "flamingo": pair("flamingo-")}

        def operator_check(policy):
            stream = StringIO()
            with (patch.object(sys, "argv", ["production_release.py", "check-native-policy"]),
                  patch.object(sys, "stdin", StringIO(json.dumps(policy))),
                  patch.object(release, "current_release", self.verify), redirect_stdout(stream)):
                release.main()
            return stream.getvalue().strip()

        for serial, record in enumerate((v1, v2, v1)):
            with self.subTest(schema=record["schema"], serial=serial):
                raw = json.dumps(record).encode()
                approval = json.dumps(self.approval | {"release_sha256": sha256(raw).hexdigest()},
                                      indent=serial).encode()
                self.sign("release.json", raw)
                self.sign("deployment.json", approval)
                digest = sha256(approval).hexdigest()
                self.assertEqual(operator_check(held), digest)
                images, receipt = self.verify()
                for stage in ("support", "gpu"):
                    active = activation["active_policy"](images, receipt, stage)
                    self.assertEqual(operator_check(active), digest)
                    if record is v1:
                        bypassed = copy.deepcopy(active)
                        bypassed["spec"]["validations"].remove(release.QWEN_ONLY)
                        with self.assertRaisesRegex(ValueError, "explicit Flamingo denial"):
                            operator_check(bypassed)
                        bypassed = copy.deepcopy(active)
                        selector = next(v for v in bypassed["spec"]["variables"] if v["name"] == "flamingo")
                        selector["expression"] = "false"
                        with self.assertRaisesRegex(ValueError, "explicit Flamingo denial"):
                            operator_check(bypassed)
                for role in images.keys() & release.ROLE_VARIABLES.keys():
                    bad = copy.deepcopy(held)
                    pin = next(v for v in bad["spec"]["variables"] if v["name"] == release.ROLE_VARIABLES[role])
                    pin["expression"] = repr(f"{release.PAIRED}@sha256:" + "0" * 64)
                    with self.assertRaisesRegex(ValueError, f"approved {role} image"):
                        operator_check(bad)
                if record is v1:
                    unheld = copy.deepcopy(held)
                    unheld["spec"]["validations"].pop(0)
                    with self.assertRaisesRegex(ValueError, "approved flamingo-trainer image"):
                        operator_check(unheld)

    def test_separate_signed_roles_bind_native_backbone(self):
        release_doc = copy.deepcopy(self.release)
        gemma = copy.deepcopy(release_doc["paired"])
        gemma["images"] = {"trainer": candidate(release.PAIRED, "d"), "preflight": candidate(release.PAIRED, "e")}
        release_doc.update(schema="nebius-image-release-v2", flamingo=gemma)
        raw = json.dumps(release_doc).encode()
        self.sign("release.json", raw)
        self.sign("deployment.json", json.dumps({**self.approval, "release_sha256": sha256(raw).hexdigest()}).encode())
        refs, _ = self.verify()
        self.assertEqual(len(set(refs.values())), 5)
        variables = [{"name": var, "expression": repr(refs[role])} for role, var in release.ROLE_VARIABLES.items()]
        policy = {"kind": "ValidatingAdmissionPolicy", "metadata": {"name": "episode-submit-inputs"},
                  "spec": {"variables": variables}}
        release.check_native_policy(refs, policy)
        with self.assertRaisesRegex(ValueError, "flamingo-trainer"):
            release.check_native_policy(release.release_refs(self.release), policy)

        def job(role, trainer, flags):
            container = {"image": refs[role], "env": [{"name": "EPISODE_IMAGE", "value": refs[trainer]}],
                         "command": ["python", "episode_job.py", *flags]}
            return {"kind": "Job", "spec": {"template": {"spec": {"containers": [container]}}}}

        for prefix, flags in (("", []), ("flamingo-", ["--flamingo-binding-sha256", "1" * 64])):
            trainer, preflight = prefix + "trainer", prefix + "preflight"
            pre, task = job(preflight, trainer, flags), job(trainer, trainer, flags)
            release.check_episode(refs, pre, task)
            other = "trainer" if prefix else "flamingo-trainer"
            for wrong_pre, wrong_task in ((job(other.replace("trainer", "preflight"), trainer, flags), task),
                                           (job(preflight, other, flags), task),
                                           (pre, job(other, trainer, flags)), (pre, job(trainer, other, flags)),
                                           (pre, job(trainer, trainer, [] if prefix else ["--flamingo-binding-sha256"]))):
                with self.assertRaises(ValueError):
                    release.check_episode(refs, wrong_pre, wrong_task)
        release_doc["flamingo"] = release_doc["paired"]
        with self.assertRaisesRegex(ValueError, "distinct"):
            release.release_refs(release_doc)

    def test_invalid_candidate_records(self):
        for field, value in (("index", "latest"), ("repository", "example.invalid/trainer")):
            data = copy.deepcopy(self.release)
            data["paired"]["images"]["trainer"][field] = value
            with self.assertRaisesRegex(ValueError, "repository or OCI digest"):
                release.release_refs(data)
        data = copy.deepcopy(self.release)
        data["paired"]["images"]["preflight"] = data["paired"]["images"]["trainer"]
        with self.assertRaisesRegex(ValueError, "distinct"):
            release.release_refs(data)
        data = copy.deepcopy(self.release)
        data["paired"]["forecast_commit"] = "main"
        with self.assertRaisesRegex(ValueError, "immutable"):
            release.release_refs(data)

    def test_rendered_fit_and_inference_check_both_image_roles(self):
        refs, _ = self.verify()
        for phase in ("fit", "infer"):
            args = [phase, "direct", "release-smoke", "--target", "prod-nebius", "--step", "1",
                    "--bundle-sha256", "d" * 64, "--image", refs["trainer"], "--preflight-image", refs["preflight"]]
            if phase == "fit":
                args += ["--schedule", "1"]
            def render(mode):
                cmd = [sys.executable, ROOT / "workloads/sky/episode-profile.py", mode, *args]
                return json.loads(subprocess.check_output(cmd))
            pre = render("preflight")
            for mode in ("job", "sky"):
                task = render(mode)
                with self.subTest(phase=phase, mode=mode):
                    release.check_episode(refs, pre, task)
                    for bad in (refs["preflight"], f"{release.PAIRED}@sha256:" + "0" * 64):
                        changed = copy.deepcopy(task)
                        if mode == "sky":
                            changed["resources"]["image_id"] = "docker:" + bad
                        else:
                            changed["spec"]["template"]["spec"]["containers"][0]["image"] = bad
                        with self.assertRaisesRegex(ValueError, "approved trainer"):
                            release.check_episode(refs, pre, changed)
                    changed = copy.deepcopy(pre)
                    changed["spec"]["template"]["spec"]["containers"][0]["image"] = refs["trainer"]
                    with self.assertRaisesRegex(ValueError, "approved preflight"):
                        release.check_episode(refs, changed, task)
