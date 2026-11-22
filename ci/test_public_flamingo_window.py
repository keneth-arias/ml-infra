"""CPU-only signed-window fixtures. No production signer or cloud APIs are used."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SKY = Path(__file__).resolve().parents[1] / "workloads" / "sky"
sys.path.insert(0, str(SKY))
import production_release as release
import public_flamingo_window as windows


def raw(data):
    return (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()


def dated(instant):
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


class WindowFixture:
    def __init__(self, directory):
        self.trust = directory / "test-trust"
        self.files = self.trust / "fixture"
        self.files.mkdir(parents=True)
        (self.trust / "current").symlink_to(self.files.name)
        self.key = directory / "test-private.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256",
                        "-out", self.key], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-out", self.trust / "signer.pub"],
                       check=True, capture_output=True)
        self.path = directory / "window.json"
        self.scope_path = directory / "scope.json"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.start, self.cutoff = dated(self.now - timedelta(minutes=15)), dated(self.now + timedelta(hours=2))
        self.scope = dict(start=self.start, cutoff=self.cutoff, stage="gpu", run_id="approved-successor-001",
                          project_id=windows.PROJECT, cluster_id=windows.CLUSTER,
                          support_instance_id="computeinstance-successor001")
        self.scope_path.write_bytes(raw(self.scope))
        self.scope_path.chmod(0o600)
        stage = {"repository": release.PAIRED, "index": "sha256:" + "c"*64,
                 "linux_amd64": "sha256:" + "d"*64, "config": "sha256:" + "e"*64}
        base = dict(stage, repository=release.ROOTLESS, index="sha256:" + "a"*64, role="rootless-cpu",
                    build_commit="a"*40)
        paired = dict(schema="paired-forecast-candidate-v1", build_commit="a"*40, forecast_commit="b"*40,
                      reader_commit="c"*40, images={"preflight": stage, "trainer": dict(stage, index="sha256:"+"1"*64)})
        self.release = {"schema": "nebius-image-release-v1", "rootless": base, "paired": paired}
        self.deployment = {"schema": "nebius-image-deployment-v1", "deployment_commit": "e"*40,
                           "cluster_id": windows.CLUSTER, "release_sha256": sha256(raw(self.release)).hexdigest()}
        self.sign(self.files / "release.json", self.release)
        self.sign(self.files / "deployment.json", self.deployment)
        self.record = dict(schema=windows.SCHEMA, allocation_run_id=self.scope["run_id"], start=self.start,
                           cutoff=self.cutoff, cpu_node=self.scope["support_instance_id"],
                           cpu_provider_id="nebius://"+self.scope["support_instance_id"],
                           cpu_node_uid="5955cf2a-54a4-4c87-bba4-34bf4bf31c70",
                           scope_sha256=sha256(raw(self.scope)).hexdigest(), deployment_commit="e"*40,
                           deployment_sha256=sha256(raw(self.deployment)).hexdigest(),
                           preflight_image=release.PAIRED+"@"+stage["index"],
                           preflight_platform_image=release.PAIRED+"@"+stage["linux_amd64"], **windows.FIXED)
        self.resign()

    def sign(self, path, value):
        data = raw(value)
        path.write_bytes(data)
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key, "-out", str(path)+".sig"],
                       input=data, capture_output=True, check=True)

    def resign(self):
        self.sign(self.path, self.record)
        self.window_sha256 = sha256(self.path.read_bytes()).hexdigest()
        self.run = "fsw-" + self.window_sha256[:32]
        self.args = SimpleNamespace(window=self.path, window_scope=self.scope_path,
                                    window_sha256=self.window_sha256, scope=None)

    def node(self):
        return {"metadata": {"name": self.record["cpu_node"], "uid": self.record["cpu_node_uid"],
                             "labels": {"airaware.training/pool": "training-cpu", "kubernetes.io/arch": "amd64",
                                        "kubernetes.io/os": "linux", windows.eligibility_label(self.run):
                                            self.record["cpu_node_uid"]}},
                "spec": {"providerID": self.record["cpu_provider_id"]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"cpu": "2"}}}


@contextmanager
def signed_fixture(directory):
    fixture = WindowFixture(Path(directory))
    with patch.object(release, "TRUST", fixture.trust):
        yield fixture


class WindowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fixture = signed_fixture(tmp.name)
        self.f = fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)

    def load(self, **kwargs):
        return windows.load_window(self.f.args, self.f.run, **kwargs)

    def test_real_signature_and_scope(self):
        window = self.load()
        self.assertEqual(window.run, self.f.run)
        self.assertEqual(window.scope_data(), self.f.scope)
        window.validate_placement(self.f.node())
        window.validate_namespace({"kind":"Namespace","metadata":{"name":"training","uid":windows.NAMESPACE_UID}})
        windows.validate_window_pin(window, {"window_sha256": self.f.window_sha256})
        with patch.dict(os.environ, NEBIUS_TRAINING_CLUSTER_ID=windows.CLUSTER, NEBIUS_CA_BUNDLE_SHA256=windows.CA):
            self.load(live=True)
        with patch.dict(os.environ, NEBIUS_TRAINING_CLUSTER_ID="wrong", NEBIUS_CA_BUNDLE_SHA256=windows.CA):
            with self.assertRaises(ValueError):
                self.load(live=True)

    def test_unsigned_changed_and_resigned_unbound_inputs(self):
        self.f.path.write_bytes(self.f.path.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.load()
        self.f.resign()
        for field, wrong in (("preflight_platform_image", self.f.record["preflight_image"]),
                             ("preflight_image", self.f.record["preflight_platform_image"]),
                             ("cpu_node_uid", "bad"), ("cpu_provider_id", "nebius://other"),
                             ("namespace_uid", "cf4803df-0405-4137-9b54-e077b327e498"),
                             ("deployment_sha256", "f"*64), ("start", self.f.cutoff),
                             ("scope_sha256", "f"*64), ("allocation_run_id", "other"),
                             ("cpu_node", "computeinstance-other"), ("objects_sha256", "f"*64)):
            with self.subTest(field=field):
                previous = self.f.record[field]
                self.f.record[field] = wrong
                self.f.resign()
                with self.assertRaises(ValueError):
                    self.load()
                self.f.record[field] = previous

    def test_wrong_live_identity_and_stale_or_future_window(self):
        w = self.load()
        node = self.f.node()
        node["metadata"]["uid"] = "7858d0bf-ed59-44d2-bdaf-9c2e99be9f16"
        with self.assertRaises(ValueError):
            w.validate_placement(node)
        key = windows.eligibility_label(w.run)
        for mark in (None, "wrong-uid"):
            with self.subTest(eligibility=mark):
                node = self.f.node()
                node["metadata"]["labels"].pop(key)
                if mark is not None:
                    node["metadata"]["labels"][key] = mark
                with self.assertRaisesRegex(ValueError, "eligibility"):
                    w.validate_placement(node)
        with patch.dict(os.environ, NEBIUS_TRAINING_CLUSTER_ID=windows.CLUSTER, NEBIUS_CA_BUNDLE_SHA256=windows.CA):
            for now in (self.f.now + timedelta(hours=2), self.f.now - timedelta(hours=2)):
                with patch.object(windows, "utcnow", return_value=now), self.assertRaises(ValueError):
                    self.load(live=True)
        with self.assertRaises(ValueError):
            windows.validate_window_pin(w, {})

    def test_legacy_and_partial_options_and_replay(self):
        parser = argparse.ArgumentParser()
        windows.add_window_args(parser)
        self.assertIsNone(windows.load_window(parser.parse_args([]), "original-run"))
        self.assertIsNone(windows.load_window(parser.parse_args([]), "fsw-preexisting"))
        with self.assertRaises(ValueError):
            windows.load_window(parser.parse_args([]), self.f.run)
        for args in (["--window", str(self.f.path)], ["--window-sha256", self.f.window_sha256],
                     ["--window-scope", str(self.f.scope_path)]):
            with self.assertRaises(ValueError):
                windows.load_window(parser.parse_args(args), "original-run")
        old_run = self.f.run
        self.f.record["cpu_node_uid"] = "7858d0bf-ed59-44d2-bdaf-9c2e99be9f16"
        self.f.resign()
        with self.assertRaises(ValueError):
            windows.load_window(self.f.args, old_run)
        self.f.args.scope = self.f.path
        with self.assertRaises(ValueError):
            self.load()

    def test_scope_alias_and_permission_checks(self):
        self.f.args.window_scope = None
        self.f.args.scope = self.f.scope_path
        self.assertEqual(self.load().scope_sha256, self.f.record["scope_sha256"])
        self.f.scope_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load()
        self.f.scope_path.chmod(0o600)
        self.f.scope_path.write_bytes(raw({**self.f.scope, "stage": "pilot"}))
        self.f.record["scope_sha256"] = sha256(self.f.scope_path.read_bytes()).hexdigest()
        self.f.resign()
        with self.assertRaises(ValueError):
            self.load()

    def test_replacement_signature_and_changed_live_release_rejected(self):
        window = self.load()
        Path(str(self.f.path) + ".sig").write_bytes((self.f.files / "release.json.sig").read_bytes())
        with self.assertRaises(ValueError):
            self.load()
        self.f.deployment["deployment_commit"] = "a" * 40
        self.f.sign(self.f.files / "deployment.json", self.f.deployment)
        with patch.dict(os.environ, NEBIUS_TRAINING_CLUSTER_ID=windows.CLUSTER, NEBIUS_CA_BUNDLE_SHA256=windows.CA):
            with self.assertRaises(ValueError):
                windows.verify_live_window(window)

    def test_original_allocation_cannot_be_reserialized_as_a_successor(self):
        original = dict(run_id="support-20261121d", start="2026-11-21T22:27:06.000Z", cutoff="2026-11-22T02:25:00Z")
        self.f.scope.update(original)
        self.f.scope_path.write_bytes(raw(self.f.scope))
        self.f.record.update(allocation_run_id=original["run_id"], start=original["start"], cutoff=original["cutoff"],
                             scope_sha256=sha256(self.f.scope_path.read_bytes()).hexdigest())
        self.f.resign()
        with self.assertRaises(ValueError):
            self.load()

    def test_unknown_image_schema_rejected_even_with_matching_signed_release(self):
        self.f.release["schema"] = "unrecognized"
        self.f.deployment["release_sha256"] = sha256(raw(self.f.release)).hexdigest()
        self.f.sign(self.f.files/"release.json", self.f.release)
        self.f.sign(self.f.files/"deployment.json", self.f.deployment)
        self.f.record["deployment_sha256"] = sha256(raw(self.f.deployment)).hexdigest()
        self.f.resign()
        with self.assertRaises(ValueError):
            self.load()


if __name__ == "__main__":
    unittest.main()
