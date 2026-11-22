import hashlib
import hmac
import io
import json
import runpy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

SKY = Path(__file__).resolve().parents[1] / "workloads/sky"
sys.path.insert(0, str(SKY))
tool = runpy.run_path(str(SKY / "mint-public-flamingo-capabilities.py"))
worker = runpy.run_path(str(SKY / "public_flamingo_stage_worker.py"))
PUBLIC = tool["MODEL_OWNER"]


class MintTests(unittest.TestCase):
    def setUp(self):
        self.key = "test-key-id"
        self.secret = "dummy-signing-secret-used-in-memory"
        rows = [{"name":name, "bucket":worker["BUCKET"], "key":f"model-qualification/{worker['WEIGHTS']}/base/{name}",
                 "size":4, "sha256":"a"*64, "version_id":"1"} for name in sorted(worker["FILES"])]
        raw = json.dumps(rows)
        self.pin = hashlib.sha256(raw.encode()).hexdigest()
        self.packet = {"objects_sha256": self.pin, "configmap":{"data":{"objects.json":raw}},
                       "project_id":tool["PROJECT"], "run":"run", "inventory_secret":"inventory"}
        self.auth = {"purpose":"mint-public-flamingo-get-capabilities", "model_owner":PUBLIC, "run":"run",
                     "project_id":tool["PROJECT"], "objects_sha256": self.pin, "inventory_secret":"inventory",
                     "ttl_seconds":300, "expires_at":(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat()}
        self.enroll = {"model_owner":PUBLIC, "project_id":tool["PROJECT"],
                       "access_key_id_sha256":hashlib.sha256(self.key.encode()).hexdigest()}

    def mint(self):
        calls=[]
        result=tool["mint"](self.packet,self.auth,self.enroll,self.key,self.secret,probe=calls.append,deny=calls.append)
        self.assertEqual(len(calls),2)
        return result

    def test_only_nine_exact_get_requests_and_separate_safe_attestation(self):
        raw, receipt, expiry = self.mint()
        self.assertNotIn(self.secret.encode(),raw)
        self.assertNotIn(self.key,json.dumps(receipt))
        self.assertNotIn("https",json.dumps(receipt))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["inventory_sha256"])
        self.assertEqual(set(receipt), {"schema","model_owner","project_id","objects_sha256","inventory_sha256","expires_at"})
        rows=json.loads(raw)["base"]
        self.assertEqual([r["name"] for r in rows],sorted(worker["FILES"]))
        for row in rows:
            parsed=urlsplit(row["url"])
            self.assertEqual(parsed.netloc,worker["HOST"])
            self.assertEqual(parsed.path,f"/{worker['BUCKET']}/model-qualification/{worker['WEIGHTS']}/base/{row['name']}")
            query=parse_qs(parsed.query)
            self.assertEqual(query["versionId"],["1"])
            self.assertEqual(query["X-Amz-SignedHeaders"],["host"])
            self.assertEqual(tool["expiration"](row["url"]),expiry)

    def test_signature_matches_get_and_rejects_put_delete_and_head(self):
        raw, _, _ = self.mint()
        for entry in json.loads(raw)["base"]:
            parsed = urlsplit(entry["url"])
            query = parse_qs(parsed.query)
            stamp = query["X-Amz-Date"][0]
            scope = stamp[:8] + "/eu-south1/s3/aws4_request"
            key = ("AWS4" + self.secret).encode()
            for piece in scope.split("/"):
                key = hmac.digest(key, piece.encode(), "sha256")
            signed_query = "&".join(sorted(pair for pair in parsed.query.split("&")
                                           if not pair.startswith("X-Amz-Signature=")))
            for method in ("GET", "PUT", "DELETE", "HEAD"):
                canonical = "\n".join((method, parsed.path, signed_query,
                                       "host:" + parsed.netloc + "\n", "host", "UNSIGNED-PAYLOAD"))
                to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, scope,
                                    hashlib.sha256(canonical.encode()).hexdigest()))
                signature = hmac.digest(key, to_sign.encode(), "sha256").hex()
                self.assertEqual(signature == query["X-Amz-Signature"][0], method == "GET")

    def test_wrong_key_and_long_term_fail_before_mint(self):
        self.enroll["access_key_id_sha256"]="0"*64
        with self.assertRaises(ValueError): self.mint()
        self.enroll["access_key_id_sha256"]=hashlib.sha256(self.key.encode()).hexdigest()
        self.auth["ttl_seconds"]=1801
        with self.assertRaises(ValueError): self.mint()

    def test_denial_needs_403_and_never_follows_body(self):
        class Reject:
            def __init__(self, code): self.code=code
            def open(self, request, timeout):
                raise HTTPError(request.full_url, self.code, "denied", {}, io.BytesIO())
        tool["require_denial"](Reject(403), "HEAD", "https://example.org/a")
        with self.assertRaises(ValueError):
            tool["require_denial"](Reject(404), "HEAD", "https://example.org/a")


if __name__ == "__main__":
    unittest.main()
