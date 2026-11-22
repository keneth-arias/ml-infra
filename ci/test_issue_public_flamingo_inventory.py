"""Exercise issuance with synthetic credentials and a local fake Secret API."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs


SKY = Path(__file__).resolve().parents[1] / "workloads/sky"
sys.path.insert(0, str(SKY))
issuer = runpy.run_path(str(SKY / "issue-public-flamingo-inventory.py"))
worker = runpy.run_path(str(SKY / "public_flamingo_stage_worker.py"))
digest = "a" * 64


class PublicInventoryTests(unittest.TestCase):
    def setUp(self):
        self.key = {"access_key_id": "dummy-key", "secret_access_key": "dummy-secret"}
        self.end = datetime.now(timezone.utc) + timedelta(hours=1)
        self.packet = {"project_id": "project-test", "objects_sha256": digest}
        self.approval = {"expires_at": self.end.isoformat(), "ttl_seconds": 150,
                         "source_operator": "source", "storage_operator": "storage", "staging_operator": "stage"}
        self.grant = {"schema": "public-flamingo-version-reader-grant-v1", "principal": "serviceaccount-test",
                      "project_id": "project-test", "access_key_id_sha256": sha256(b"dummy-key").hexdigest(),
                      "objects_sha256": digest, "exact_versions_only": True,
                      "permissions": ["s3:GetObjectVersion"], "expires_at": self.end.isoformat()}

    def test_explicit_null_cannot_disable_independent_grant_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grant"
            path.write_bytes(b"{}")
            path.chmod(0o600)
            self.assertEqual(issuer["read_private"](path), {})
            with self.assertRaises(ValueError):
                issuer["read_private"](path, None)

    def test_grant_must_bind_exact_credentials_objects_and_action(self):
        check = issuer["check_grant"]
        with patch.dict(os.environ, {}, clear=True):
            check(self.grant, self.key, self.packet, self.approval)
            for change in ({"permissions": ["s3:GetObject"]}, {"exact_versions_only": 1},
                           {"objects_sha256": "b"*64}, {"access_key_id_sha256": "c"*64}):
                with self.assertRaises(ValueError):
                    check(dict(self.grant, **change), self.key, self.packet, self.approval)
            os.environ["AWS_PROFILE"] = "admin"
            with self.assertRaises(ValueError):
                check(self.grant, self.key, self.packet, self.approval)

    def test_versioned_gets_match_worker_and_expiry(self):
        row = {"bucket": "airaware-artifacts-test", "key": "model/base/a.json", "name": "a.json",
               "sha256": digest, "version_id": "1", "size": 37}
        rows = issuer["presign"]([row], self.key, 150, self.end)
        url = urlsplit(rows[0]["url"])
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "storage.eu-south1.nebius.cloud", "/airaware-artifacts-test/model/base/a.json"))
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        worker["validate_query"](query, "1")
        self.assertEqual(query["X-Amz-Expires"], "150")
        self.assertEqual(set(rows[0]), {"name", "sha256", "version_id", "size", "url"})
        for duration, end in [(0,self.end),(1801,self.end),(150,datetime.now(timezone.utc))]:
            with self.assertRaises(ValueError):
                issuer["presign"]([row], self.key, duration, end)

    def test_secret_create_returns_only_identity_and_rejects_ambiguous_result(self):
        fn = issuer["create_secret"]
        uid = "9685acff-3081-4fec-9490-e6a9878cb775"
        prefix = ["/usr/local/bin/kubectl"]
        with patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, uid + " 123", "")
            reply = fn(prefix, "stage-test-inventory", [{"url":"private-test"}])
            self.assertEqual(reply, {"namespace":"training","name":"stage-test-inventory",
                                     "uid":uid,"resourceVersion":"123"})
            args, kwargs = run.call_args
            self.assertNotIn("private-test", repr(args))
            secret = json.loads(kwargs["input"])
            self.assertTrue(secret["immutable"])
            self.assertEqual(args[0][args[0].index("create"):][:3], ["create", "-f", "-"])
            run.return_value = subprocess.CompletedProcess([], 1, "", "private-test")
            with self.assertRaisesRegex(RuntimeError,"inspect") as exc:
                fn(prefix, "stage-test-inventory", [])
            self.assertNotIn("private-test",str(exc.exception))


if __name__ == "__main__":
    unittest.main()
