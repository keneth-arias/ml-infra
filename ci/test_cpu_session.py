"""Exercise the attended CPU credential lifecycle without retaining tokens."""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "bootstrap/nebius/management/cpu-session.py"
spec = importlib.util.spec_from_file_location("cpu_session", MODULE)
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)
UID = "b2b83e58-b49d-416d-bdf7-dd6ee87d1273"
TARGET = dict(endpoint="public", cluster_id="mk8scluster-fixture", ca_bundle_sha256="a" * 64)


class Cluster:
    def __init__(self, identity="sky-backend"):
        self.identity = identity
        self.marker = None
        self.uid = None
        self.deleted = []
        self.token_calls = []

    def kubectl(self, client, *args, payload=None):
        if args[0] == "create":
            self.marker = payload
            self.uid = UID
            return UID
        if args[:4] == ("-n", "sky-cpu", "create", "token"):
            self.token_calls.append(args)
            assert "--bound-object-name=" + self.marker["metadata"]["name"] in args
            assert "--bound-object-uid=" + UID in args and "--bound-object-kind=Secret" in args
            return json.dumps(
                {
                    "spec": {"audiences": ["fixture-api"]},
                    "status": {
                        "token": "synthetic-token",
                        "expirationTimestamp": (
                            datetime.now(timezone.utc) + timedelta(minutes=15)
                        ).isoformat(),
                    },
                }
            )
        if args[:2] == ("auth", "whoami"):
            path = Path(client[client.index("--kubeconfig") + 1])
            cfg = json.loads(path.read_bytes())
            assert cfg["users"][0]["user"] == {"token": "synthetic-token"} and len(cfg["users"]) == 1
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            return f"system:serviceaccount:sky-cpu:{self.identity}"
        if args[:4] == ("-n", "sky-cpu", "get", "secret"):
            return self.uid or ""
        if args[:2] == ("delete", "--raw"):
            assert args[2].endswith("/sky-cpu/secrets/" + self.marker["metadata"]["name"])
            assert payload["preconditions"] == {"uid": self.uid}
            self.deleted.append(self.uid)
            self.uid = None
            return "{}"
        raise AssertionError(args)


class CPUSessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "lease"

    def test_bound_credential_and_repeat_close(self):
        cluster = Cluster()
        with patch.object(session, "kubectl", cluster.kubectl):
            receipt = session.issue(["kubectl"], b"ca", TARGET, self.path, "sky-backend", 900)
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
            self.assertEqual(cluster.marker["type"], "Opaque")
            self.assertTrue(cluster.marker["immutable"])
            self.assertNotIn("data", cluster.marker)
            for name in ("intent.json", "issued.json", "marker.json"):
                self.assertNotIn("synthetic-token", (self.path / name).read_text())
            self.assertEqual(receipt["audiences"], ["fixture-api"])
            session.revoke(["kubectl"], receipt)
            session.revoke(["kubectl"], receipt)
            self.assertEqual(cluster.deleted, [UID])

    def test_bad_identity_rolls_back_only_its_own_marker(self):
        cluster = Cluster(identity="wrong")
        with (
            patch.object(session, "kubectl", cluster.kubectl),
            self.assertRaisesRegex(ValueError, "identity"),
        ):
            session.issue(["kubectl"], b"ca", TARGET, self.path, "cpu-submit", 900)
        self.assertEqual(cluster.deleted, [UID])
        self.assertFalse((self.path / "kubeconfig").exists())
        self.assertFalse((self.path / "pending").exists())

    def test_replacement_and_unknown_deletion_outcome(self):
        cluster = Cluster()
        with patch.object(session, "kubectl", cluster.kubectl):
            receipt = session.issue(["kubectl"], b"ca", TARGET, self.path, "sky-backend", 900)

        def dropped(client, *args, payload=None):
            if args[:2] == ("delete", "--raw"):
                raise subprocess.CalledProcessError(1, "kubectl")
            return cluster.kubectl(client, *args, payload=payload)

        with patch.object(session, "kubectl", dropped), self.assertRaises(subprocess.CalledProcessError):
            session.revoke(["kubectl"], receipt)
        self.assertTrue((self.path / "marker.json").exists())
        cluster.uid = "6e37ce32-3e0d-4068-ba49-d8bfea1cb92e"
        with patch.object(session, "kubectl", cluster.kubectl):
            session.revoke(["kubectl"], receipt)
        self.assertFalse(cluster.deleted)

    def test_rollback_attempts_both_cleanups_preserving_the_original_error(self):
        cluster = Cluster(identity="wrong")
        with (
            patch.object(session, "kubectl", cluster.kubectl),
            patch.object(Path, "unlink", side_effect=PermissionError("synthetic")),
            self.assertRaisesRegex(ValueError, "unexpected identity") as errors,
        ):
            session.issue(["kubectl"], b"ca", TARGET, self.path, "cpu-submit", 900)
        self.assertEqual(cluster.deleted, [UID])
        self.assertTrue(any("pending file" in note for note in errors.exception.__notes__))

    def test_revoke_waits_for_401_and_refuses_inconclusive_status(self):
        from unittest.mock import Mock

        self.path.write_text(json.dumps({"users": [{"user": {"token": "synthetic"}}]}))
        with (
            patch.object(session.ssl, "SSLContext"),
            patch.object(session.time, "sleep") as pause,
            patch.object(session, "HTTPSConnection") as conn,
        ):
            instance = conn.return_value
            instance.getresponse.side_effect = [Mock(status=201), Mock(status=401), Mock(status=403)]
            session.wait_unauthorized(TARGET, b"fixture", self.path)
            pause.assert_called_once()
            self.assertEqual(instance.close.call_count, 2)
            with self.assertRaisesRegex(ValueError, "403"):
                session.wait_unauthorized(TARGET, b"fixture", self.path)
