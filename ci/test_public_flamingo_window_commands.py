"""Exercise the actual staging command parsers with disposable issuer and deployment keys."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_public_flamingo_window import signed_fixture, SKY


class WindowCommands(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.fixture = self.enterContext(signed_fixture(self.directory))
        self.window_args = ["--window", str(self.fixture.path), "--window-sha256", self.fixture.window_sha256,
                            "--window-scope", str(self.fixture.scope_path)]

    def test_each_command_rejects_an_untrusted_window_before_accessing_target(self):
        f = self.fixture
        # Keep the approved bytes and digest intact. Damage only the detached test signature.
        Path(str(f.path) + ".sig").write_bytes(b"invalid signature")
        approval = self.directory / "approval.json"
        approval.write_text(json.dumps({"run": f.run}))
        approval.chmod(0o600)
        approved = ["--approval", str(approval), "--approved-sha256", sha256(approval.read_bytes()).hexdigest()]
        inputs = ["--promotion", "unused", "--binding", "unused"]
        scope = ["--scope", str(f.scope_path)]
        common = ["--packet", "unused", *inputs, *scope, *approved]
        render = [*inputs, "--expected-deployment", f.record["deployment_sha256"], "--run", f.run,
                  "--inventory-secret", "stage-" + f.run + "-inventory"]
        uid = "6780a6e4-7eea-442f-a965-4175be01ae8f"
        close = [*common, "--run", f.run, "--expected-deployment", f.record["deployment_sha256"],
                 "--packet-sha256", "unused", "--job-uid", uid, "--configmap-uid", uid, "--inventory-uid", uid]
        commands = [
            ("render-public-flamingo-stage.py", render),
            ("public_flamingo_stage_guard.py", ["--run", f.run, "--phase", "closed"]),
            ("check-public-flamingo-consumers.py", ["--run", f.run, "--closed-pins", "unused",
                                                   "--closed-pins-sha256", "unused"]),
            ("issue-public-flamingo-inventory.py", [*common, "--reader-grant", "unused",
                                                   "--reader-credentials", "unused", "--consumer-check", "unused"]),
            ("public-flamingo-stage-closeout.py", ["capture", *close]),
            ("public-flamingo-stage-closeout.py", ["fence", *close, "--capture", "unused", "--closure", "unused",
                                                 "--capture-sha256", "unused", "--closure-sha256", "unused"]),
        ]
        for action in ("enroll", "activate"):
            commands.append(("submit-public-flamingo-stage.py", [action, *common]))
            commands.append(("prepare-public-flamingo-leaves.py", [action, *common, "--owner-job", "unused"]))
        for filename, arguments in commands:
            with self.subTest(command=filename, action=arguments[0]):
                with patch.object(sys, "argv", [filename, *arguments, *self.window_args]):
                    with self.assertRaisesRegex(ValueError, "Untrusted signature"):
                        runpy.run_path(str(SKY / filename))["main"]()

    def test_concurrent_independent_guard_renders_preserve_evidence(self):
        # Exercise the operator's per-attempt directory and no-clobber convention with two
        # separate interpreters and the same window. Neither runner shares mutable output.
        fixture_code = """
import runpy, sys
from pathlib import Path
sky, trust, *sys.argv = sys.argv[1:]
sys.path.insert(0, sky)
import production_release
production_release.TRUST = Path(trust)
runpy.run_path(sys.argv[0], run_name="__main__")
"""
        def render(_):
            attempt = Path(tempfile.mkdtemp(prefix=self.fixture.run + ".", dir=self.directory))
            out = attempt / "closed-guard.json"
            command = [sys.executable, "-I", "-B", "-c", fixture_code, str(SKY), str(self.fixture.trust),
                       str(SKY / "public_flamingo_stage_guard.py"), "--run", self.fixture.run, "--phase", "closed",
                       *self.window_args]
            with out.open("xb") as stream:
                subprocess.run(command, stdout=stream, check=True, timeout=20)
            return out

        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(render, range(2))
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        policy, binding = json.loads(first.read_bytes())["items"]
        self.assertEqual(policy["metadata"]["name"], binding["spec"]["policyName"])
        self.assertIn(self.fixture.window_sha256, policy["spec"]["auditAnnotations"][0]["valueExpression"])
        self.assertEqual(binding["spec"]["validationActions"], ["Deny"])


if __name__ == "__main__":
    unittest.main()
