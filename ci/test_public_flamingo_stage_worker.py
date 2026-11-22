import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import urlencode
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workloads/sky"))
import public_flamingo_stage_worker as worker


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    status = 200

    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data)), "x-amz-version-id": "1"}


class StageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.base, self.output = root / "base", root / "output"
        self.base.mkdir()
        self.output.mkdir()
        self.program, self.source = root / "objects.json", root / "inventory.json"
        self.data = {name: name.encode() for name in worker.FILES if name != "SHA256SUMS"}
        sums = "".join(f"{digest(data)}  {name}\n" for name, data in sorted(self.data.items()))
        self.data["SHA256SUMS"] = sums.encode()
        self.weights = digest(sums.encode())
        patched = patch.multiple(worker, UID=os.geteuid(), WEIGHTS=self.weights)
        patched.start()
        self.addCleanup(patched.stop)
        self.rows, self.inventory = [], []
        self.urls = {}
        for name, data in sorted(self.data.items()):
            key = f"model-qualification/{self.weights}/base/{name}"
            date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            query = urlencode({"versionId": "1", "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                               "X-Amz-Credential": f"fixture/{date[:8]}/eu-south1/s3/aws4_request",
                               "X-Amz-Date": date, "X-Amz-Expires": "900", "X-Amz-SignedHeaders": "host",
                               "X-Amz-Signature": "f" * 64})
            url = f"https://{worker.HOST}/{worker.BUCKET}/{key}?{query}"
            entry = dict(name=name, size=len(data), sha256=digest(data), version_id="1")
            self.rows.append(dict(entry, bucket=worker.BUCKET, key=key))
            self.inventory.append(dict(entry, url=url))
            self.urls[url] = data
        self.program.write_bytes(json.dumps(self.rows).encode())
        self.pin = digest(self.program.read_bytes())
        self.source.write_text(json.dumps({"base": self.inventory}))
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        return Response(self.urls[url])

    def stage(self):
        return worker.stage(self.pin, self.program, self.source, self.base, self.output, self.fetch)

    def test_publish_and_verify_offline_hit(self):
        expected = dict(weights_manifest_sha256=self.weights, output_uid=os.geteuid(), output_empty=True)
        self.assertEqual(self.stage(), expected)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual(self.stage(), expected)
        self.assertEqual(len(self.calls), 9)
        self.assertEqual({p.name for p in self.base.iterdir()}, {"weights", "SHA256SUMS"})
        self.assertEqual((self.base / "SHA256SUMS").read_bytes(), self.data["SHA256SUMS"])

    def test_unverified_response_preserves_unpublished_state_and_retry(self):
        url = self.inventory[-1]["url"]
        self.urls[url] = b"x" * len(self.urls[url])
        with self.assertRaises(ValueError):
            self.stage()
        self.assertFalse((self.base / "SHA256SUMS").exists())
        self.urls[url] = self.data[self.inventory[-1]["name"]]
        self.stage()

    def test_resume_between_renames(self):
        rename = os.rename

        def interrupt(source, target, *args, **kwargs):
            if Path(target) == self.base / "SHA256SUMS":
                raise OSError("interrupted")
            return rename(source, target, *args, **kwargs)

        with patch("os.rename", side_effect=interrupt), self.assertRaises(OSError):
            self.stage()
        self.assertTrue((self.base / "weights").is_dir())
        self.assertFalse((self.base / "SHA256SUMS").exists())
        self.calls.clear()
        self.stage()
        self.assertEqual(self.calls, [])

    def test_reject_foreign_or_linked_files_and_busy_writer(self):
        outside = self.output.parent / "outside"
        outside.write_text("retained")
        extra = self.base / "other-run"
        extra.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(outside.read_text(), "retained")
        extra.unlink()
        with contextlib.ExitStack() as stack:
            fd = os.open(self.base, os.O_DIRECTORY)
            stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                self.stage()
        self.stage()
        victim = self.base / self.rows[-1]["name"]
        os.link(victim, outside.parent / "duplicate")
        with self.assertRaises(ValueError):
            self.stage()

    def test_reject_source_substitution_before_writes_or_network(self):
        original = self.inventory[0]["url"]
        for url in (original.replace(worker.HOST, "other.example"),
                    original.replace(worker.BUCKET, "other-bucket"), original + "&versionId=1",
                    original.replace("versionId=1", "versionId=2"), original + "#extra", original + "&acl=",
                    original.replace("Expires=900", "Expires=1801"), original.replace("eu-south1%2Fs3", "other%2Fs3"),
                    original.split("&X-Amz")[0]):
            with self.subTest(url=url):
                self.inventory[0]["url"] = url
                self.source.write_text(json.dumps({"base": self.inventory}))
                with self.assertRaises(ValueError):
                    self.stage()
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_reject_nonempty_output_and_wrong_program_pin(self):
        self.pin = "f" * 64
        with self.assertRaises(ValueError):
            self.stage()
        self.pin = digest(self.program.read_bytes())
        (self.output / "previous-run").touch()
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_fail_closed_on_corrupt_published_file(self):
        self.stage()
        f = self.base / self.rows[-1]["name"]
        f.write_bytes(b"x" * f.stat().st_size)
        with self.assertRaises(ValueError):
            self.stage()
        self.assertEqual(len(self.calls), 9)

    def test_network_error_never_exposes_version_url(self):
        def fail(url):
            raise OSError("private signed query " + url)

        with self.assertRaises(ValueError) as failure:
            worker.stage(self.pin, self.program, self.source, self.base, self.output, fail)
        self.assertNotIn("private signed", str(failure.exception))
        self.assertNotIn("versionId", str(failure.exception))
        self.assertFalse((self.base / "SHA256SUMS").exists())

    def test_reject_wrong_version_and_short_or_oversized_response(self):
        for scenario in ("wrong-version", "short", "oversized"):
            with self.subTest(scenario=scenario):
                def bad_fetch(url):
                    data = self.urls[url]
                    if scenario == "wrong-version":
                        response = Response(data)
                        response.headers["x-amz-version-id"] = "2"
                    else:
                        response = Response(data[:-1] if scenario == "short" else data + b"x")
                        response.headers["Content-Length"] = str(len(data))
                    return response

                with self.assertRaises(ValueError):
                    worker.stage(self.pin, self.program, self.source, self.base, self.output, bad_fetch)
                self.assertFalse((self.base / "SHA256SUMS").exists())

    def test_manifest_must_match_file_hashes_recorded_in_objects(self):
        name = self.rows[-1]["name"]
        body = b"wrong but pinned"
        url = self.inventory[-1]["url"]
        self.urls[url] = body
        for row in (self.inventory[-1], self.rows[-1]):
            row.update(sha256=digest(body), size=len(body))
        self.program.write_text(json.dumps(self.rows))
        self.pin = digest(self.program.read_bytes())
        self.source.write_text(json.dumps({"base": self.inventory}))
        with self.assertRaisesRegex(ValueError, "SHA256SUMS"):
            self.stage()
        self.assertFalse((self.base / "SHA256SUMS").exists())
        self.assertNotEqual(self.urls[url], self.data[name])

    def test_reject_wrong_leaf_owner(self):
        with patch.object(worker, "UID", os.geteuid() + 1), patch("os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(ValueError, "owned"):
                self.stage()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_reject_symlink_named_like_a_staged_weight(self):
        self.stage()
        target = self.base / self.rows[-1]["name"]
        other = self.output.parent / "unrelated"
        target.rename(other)
        target.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "links"):
            self.stage()
        self.assertTrue(other.is_file())
