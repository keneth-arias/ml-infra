"""La publicación horaria pertenece a una rama y requiere revisión de qriqs."""

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

WORKLOAD = Path(__file__).resolve().parents[1] / "workloads/web-refresh"
spec = importlib.util.spec_from_file_location("web_review", WORKLOAD / "open-review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)
HEAD = "a" * 40


class WebReviewTests(unittest.TestCase):
    def pr(self, state="open", reviewers=()):
        return {"number": 8, "state": state, "head": {"sha": HEAD},
                "requested_reviewers": [{"login": user} for user in reviewers]}

    @patch.object(review, "gh")
    def test_crea_pr_y_solicita_revision_sin_fusionar(self, gh):
        gh.side_effect = ["[]", HEAD, json.dumps({"ahead_by": 1, "files": [
            {"filename": "public/datos/actual.json", "status": "modified"}]}),
            json.dumps(self.pr()), "", ""]
        self.assertEqual(review.open_review(HEAD), 8)
        calls = [call.args for call in gh.call_args_list]
        self.assertIn("base=main", calls[3])
        self.assertIn("reviewers[]=qriqs", calls[-1])
        self.assertFalse(any("merge" in arg for call in calls for arg in call))

    @patch.object(review, "gh")
    def test_reintentos_respetan_revision_y_cierre(self, gh):
        for pr in (self.pr(reviewers=["qriqs"]), self.pr(state="closed")):
            gh.reset_mock()
            gh.return_value = json.dumps([pr])
            self.assertEqual(review.open_review(HEAD), 8)
            self.assertEqual(gh.call_count, 1)

    @patch.object(review, "gh")
    def test_rechaza_cambios_de_codigo(self, gh):
        gh.side_effect = ["[]", HEAD, json.dumps({"ahead_by": 1, "files": [
            {"filename": "src/app/page.tsx", "status": "modified"}]})]
        with self.assertRaises(ValueError):
            review.open_review(HEAD)
        self.assertEqual(gh.call_count, 3)

    def test_pod_solo_sube_a_rama_de_pr(self):
        pushes = [s.strip() for s in (WORKLOAD / "run.sh").read_text().splitlines()
                  if s.strip().startswith("git push ")]
        self.assertEqual(pushes, ['git push origin "HEAD:refs/heads/$branch"'])
