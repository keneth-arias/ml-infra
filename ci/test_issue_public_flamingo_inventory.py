"""Exercise issuance with synthetic credentials and a local fake Secret API."""

from datetime import datetime, timedelta, timezone
from email.message import Message
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs


SKY = Path(__file__).resolve().parents[1] / "workloads/sky"
sys.path.insert(0, str(SKY))
issuer = runpy.run_path(str(SKY / "issue-public-flamingo-inventory.py"))
worker = runpy.run_path(str(SKY / "public_flamingo_stage_worker.py"))
digest = "a" * 64


def promoted_objects():
    return [{"name": name, "size": i + 1, "version_id": f"test-version-{i}", "sha256": digest,
             "bucket": worker["BUCKET"], "key": f"model-qualification/{worker['WEIGHTS']}/base/{name}"}
            for i, name in enumerate(sorted(worker["FILES"]))]


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

    def test_scoped_inventory_requires_exact_ordered_live_gets(self):
        rows = promoted_objects()
        inventory = issuer["presign"](rows, self.key, 150, self.end)
        check = issuer["check_capability_inventory"]
        self.assertEqual(check({"base": inventory}, rows, 150, self.end), inventory)
        first = inventory[0]
        url = first["url"]
        for change in (
            {"size": True}, {"sha256": "b" * 64}, {"version_id": "other"},
            {"url": url.replace(worker["HOST"], "other.invalid")},
            {"url": url.replace(worker["BUCKET"], "other-bucket")},
            {"url": url.replace(rows[0]["key"], rows[1]["key"])},
            {"url": url.replace("versionId=test-version-0", "versionId=other")},
            {"url": url.replace("AWS4-HMAC-SHA256", "other")},
            {"url": url + "&versionId=test-version-0"},
            {"url": url + "#fragment"},
        ):
            with self.subTest(fields=list(change)), self.assertRaises(ValueError):
                check({"base": [dict(first, **change), *inventory[1:]]}, rows, 150, self.end)
        for invalid in (inventory[:-1], inventory[::-1], [first, *inventory[:-1]]):
            with self.assertRaises(ValueError):
                check({"base": invalid}, rows, 150, self.end)
        with self.assertRaises(ValueError):
            check({"base": inventory}, rows, 149, self.end)
        with self.assertRaises(ValueError):
            check({"base": inventory}, rows, 150, datetime.now(timezone.utc) + timedelta(seconds=10))

    def test_scoped_attestation_pins_inventory_without_a_reader_grant(self):
        claim = {"schema": "public-flamingo-scoped-get-attestation-v1", "model_owner": "model-owner",
                 "project_id": self.packet["project_id"], "objects_sha256": digest,
                 "inventory_sha256": digest, "expires_at": self.end.isoformat()}
        check = issuer["check_capability_attestation"]
        with patch.dict(os.environ, {"AWS_PROFILE": "unused"}, clear=True):
            check(claim, self.packet, self.approval)
            for change in ({"model_owner": "source"}, {"objects_sha256": "b" * 64},
                           {"inventory_sha256": None}, {"permissions": ["s3:GetObjectVersion"]}):
                with self.assertRaises(ValueError):
                    check(dict(claim, **change), self.packet, self.approval)
            with self.assertRaises(ValueError):
                check(claim, self.packet, dict(self.approval, storage_operator="source"))

    def test_range_probe_checks_nine_gets_with_exact_headers_and_body(self):
        inventory = issuer["presign"](promoted_objects(), self.key, 150, self.end)

        def probe(request, timeout):
            row = next(row for row in inventory if row["url"] == request.full_url)
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Range"), "bytes=0-0")
            self.assertEqual(timeout, 10)
            response = BytesIO(b"x")
            response.status = 206
            response.headers = Message()
            response.headers["x-amz-version-id"] = row["version_id"]
            response.headers["Content-Range"] = f"bytes 0-0/{row['size']}"
            return response

        def bad_version(request, timeout):
            response = probe(request, timeout)
            response.headers.replace_header("x-amz-version-id", "other")
            return response

        fn = issuer["probe_versions"]
        opener = Mock()
        with patch.dict(fn.__globals__, {"build_opener": opener}):
            opener.return_value.open.side_effect = probe
            fn(inventory)
            self.assertEqual(opener.return_value.open.call_count, 9)
            proxy, https, redirect = opener.call_args.args
            self.assertEqual(proxy.proxies, {})
            self.assertIsNone(https._context.keylog_filename)
            self.assertIsNone(redirect.redirect_request(None, None, 302, None, None, "https://other.invalid"))
            opener.return_value.open.side_effect = bad_version
            with self.assertRaises(ValueError):
                fn(inventory)

    def test_capability_cli_binds_private_inputs_and_checks_guard_on_both_sides(self):
        rows = promoted_objects()
        inventory = issuer["presign"](rows, self.key, 150, self.end)
        packet = dict(self.packet, cluster_id="test-cluster", run="test", inventory_secret="stage-test-inventory",
                      configmap={"data": {"objects.json": json.dumps(rows)}})
        scope = {"cluster_id": packet["cluster_id"], "project_id": packet["project_id"],
                 "cutoff": self.end.isoformat()}
        closed = {"policy_generation": 1}
        observer = {"schema": "public-flamingo-consumer-check-v1", "cluster_id": packet["cluster_id"],
                    "run": packet["run"], "inventory_secret": packet["inventory_secret"], "closed_guard": closed,
                    "storage_operator": self.approval["storage_operator"], "secret_absent": True,
                    "no_live_consumers": True, "observed_at": datetime.now(timezone.utc).isoformat()}
        with tempfile.TemporaryDirectory() as directory:
            argv = ["issuer"]

            def private(name, document):
                path = Path(directory) / name
                payload = json.dumps(document).encode()
                path.write_bytes(payload)
                path.chmod(0o600)
                argv.extend(("--" + name, str(path)))
                return sha256(payload).hexdigest()

            inventory_pin = private("capability-inventory", {"base": inventory})
            attestation_pin = private("capability-attestation", {
                "schema": "public-flamingo-scoped-get-attestation-v1", "model_owner": "model-owner",
                "project_id": packet["project_id"], "objects_sha256": packet["objects_sha256"],
                "inventory_sha256": inventory_pin, "expires_at": self.end.isoformat(),
            })
            approval_pin = private("approval", dict(
                self.approval, purpose="issue-public-flamingo-inventory", closed_guard=closed,
                deployment_sha256=digest, run=packet["run"], inventory_secret=packet["inventory_secret"],
                packet_file_sha256=private("packet", packet), consumer_check_sha256=private("consumer-check", observer),
                capability_attestation_sha256=attestation_pin,
            ))
            scope_pin = private("scope", scope)
            argv.extend(("--promotion", "unused", "--binding", "unused", "--approved-sha256", approval_pin))
            actions = Mock()
            actions.create.return_value = {"name": packet["inventory_secret"], "namespace": "training",
                                           "uid": "9685acff-3081-4fec-9490-e6a9878cb775", "resourceVersion": "1"}
            main = issuer["main"]
            with patch.object(sys, "argv", argv), patch.object(sys, "stdout", new_callable=StringIO) as stdout:
                with patch.dict(main.__globals__, {"RENDER": Mock(return_value=packet), "SCOPE_SHA": scope_pin,
                                                   "source_client": Mock(return_value=[]), "verify_guard": actions.guard,
                                                   "probe_versions": actions.probe, "create_secret": actions.create}):
                    main()
                    self.assertEqual(json.loads(stdout.getvalue()), actions.create.return_value)
                    self.assertEqual([call[0] for call in actions.mock_calls], ["guard", "probe", "guard", "create"])
                    for call in actions.guard.call_args_list:
                        self.assertEqual(call.args[1:], ("test", "closed", closed))

                    actions.reset_mock()
                    actions.guard.side_effect = [None, ValueError("guard changed")]
                    with self.assertRaises(ValueError):
                        main()
                    actions.create.assert_not_called()

                    actions.reset_mock()
                    capability_path = Path(directory) / "capability-inventory"
                    capability_path.write_bytes(capability_path.read_bytes() + b" ")
                    with self.assertRaises(ValueError):
                        main()
                    self.assertEqual(actions.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
