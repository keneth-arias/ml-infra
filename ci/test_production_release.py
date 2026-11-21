import copy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

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
                  "spec": {"variables": variables}}
        release.check_native_policy(images, policy)
        variables[0]["expression"] = repr(images["preflight"])
        with self.assertRaisesRegex(ValueError, "approved trainer image"):
            release.check_native_policy(images, policy)
        variables[0]["expression"] = repr(images["trainer"])
        variables.pop()
        with self.assertRaisesRegex(ValueError, "approved preflight image"):
            release.check_native_policy(images, policy)

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
