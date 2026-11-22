import copy
import json
from pathlib import Path
import runpy
import unittest


ROOT = Path(__file__).resolve().parents[1]
verify = runpy.run_path(str(ROOT / "ci/verify-paired-image.py"))["verify"]


class PairedImageTest(unittest.TestCase):
    def test_distinct_indexes_cannot_hide_a_shared_platform_or_configuration(self):
        receipt = json.loads((ROOT / "bootstrap/nebius/releases/flamingo-candidate.json").read_text())
        for field in ("index", "linux_amd64", "config"):
            with self.subTest(field=field):
                changed = copy.deepcopy(receipt)
                trainer, preflight = changed["images"]["trainer"], changed["images"]["preflight"]
                preflight[field] = trainer[field]
                if field == "linux_amd64":
                    preflight["config"] = trainer["config"]
                with self.assertRaisesRegex(ValueError, f"distinct {field} digest"):
                    verify(Path("unused"), changed, "trainer")


if __name__ == "__main__":
    unittest.main()
