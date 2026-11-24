import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publish import TARGET, git, publish_snapshot


def snapshot(generated, observed):
    return {
        "schema_version": 1,
        "model": {"name": "test", "checkpoint_sha256": "a" * 64},
        "generated_at_utc": generated,
        "timeline": [observed],
        "stations": {"sample": {"windows": [{"forecast": [1, 2, 3, 4, 5, 6]}]}},
    }


class PublishTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.remote = self.root / "remote.git"
        self.remote.mkdir()
        git(self.remote, "init", "--bare", "--initial-branch=main")
        self.other = self.clone("writer")
        self.old = snapshot("2026-11-24T01:00:00+00:00", "2026-11-23T19:00:00")
        (self.other / TARGET).parent.mkdir(parents=True)
        (self.other / TARGET).write_text(json.dumps(self.old))
        self.advance(self.other)
        self.publisher = self.clone("publisher")
        self.candidate = self.root / "actual.json"
        self.new = snapshot("2026-11-24T01:15:00+00:00", "2026-11-23T20:00:00")
        self.candidate.write_text(json.dumps(self.new))

    def clone(self, name):
        git(self.root, "clone", "--depth=1", self.remote.as_uri(), name)
        return self.root / name

    def advance(self, repo):
        git(repo, "add", ".")
        git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.org",
            "commit", "-m", "update")
        git(repo, "push", "origin", "HEAD:refs/heads/main")

    def publish(self):
        return publish_snapshot(self.publisher, self.candidate, "test", "a" * 64)

    def test_revalidates_after_other_commit_without_losing_it(self):
        def moved_head(repo, *args):
            if args[0] == "push" and not (self.other / "keep.txt").exists():
                (self.other / "keep.txt").write_text("another writer")
                self.advance(self.other)
            return git(repo, *args)

        with patch("publish.git", side_effect=moved_head):
            published = self.publish()
        self.assertEqual(published, git(self.remote, "rev-parse", "main"))
        self.assertEqual(git(self.remote, "show", "main:keep.txt"), "another writer")
        self.assertEqual(json.loads(git(self.remote, "show", f"main:{TARGET}")), self.new)

    def test_stale_candidate_leaves_head_untouched(self):
        self.candidate.write_text(json.dumps(self.old))
        head = git(self.remote, "rev-parse", "main")
        self.assertIsNone(self.publish())
        self.assertEqual(git(self.remote, "rev-parse", "main"), head)

    def test_fresh_capture_cannot_regress_observation_hour(self):
        self.new["timeline"][-1] = "2026-11-23T18:00:00"
        self.candidate.write_text(json.dumps(self.new))
        head = git(self.remote, "rev-parse", "main")
        with self.assertRaisesRegex(SystemExit, "hora observada retrocedió"):
            self.publish()
        self.assertEqual(git(self.remote, "rev-parse", "main"), head)
