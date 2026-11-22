import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAUNCH = Path(__file__).resolve().parents[1] / "workloads/sky/launch-episode.sh"


class EpisodeLaunchTest(unittest.TestCase):
    def launch(self, mode, resources, context=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "calls.jsonl"
            kubectl = root / "kubectl"
            kubectl.write_text(f"""#!{sys.executable}
import json
import os
import sys

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
with open(os.environ["KUBE_CALLS"], "a") as log:
    log.write(json.dumps([name, *args]) + "\\n")
if name == "sky":
    if args[0] == "status":
        print("[]")
elif "get" in args:
    print(os.environ["BM_RESOURCES"])
elif "create" in args:
    print("job/preflight")
""")
            kubectl.chmod(0o755)
            (root / "sky").symlink_to(kubectl)
            env = {
                **os.environ,
                "PATH": f"{root}:{os.environ['PATH']}",
                "KUBECONFIG": str(root / "user-config-with-nebius-default"),
                "KUBE_CALLS": str(log),
                "BM_RESOURCES": resources,
            }
            env.pop("EPISODE_LOCAL_CONTEXT", None)
            if context:
                env["EPISODE_LOCAL_CONTEXT"] = context
            result = subprocess.run(
                [
                    LAUNCH, mode, "infer", "direct", "station-trial",
                    "--image", "registry/test@sha256:" + "a" * 64,
                    "--bundle-sha256", "b" * 64, "--step", "14838",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_native_launch_checks_and_uses_the_local_context(self):
        for resources, submits in (("True 2", True), ("False 2", False), ("True 0", False)):
            with self.subTest(resources=resources):
                result, calls = self.launch("job", resources)
                self.assertTrue(all(call[:3] == ["kubectl", "--context", "microk8s"] for call in calls))
                self.assertIn("bm", calls[0])
                self.assertEqual(
                    [next(word for word in call if word in {"get", "create", "wait"}) for call in calls],
                    ["get", "create", "wait", "create"] if submits else ["get"],
                )
                if submits:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(calls[1][calls[1].index("-f") + 1].endswith("/pre.json"))
                    self.assertTrue(calls[3][calls[3].index("-f") + 1].endswith("/task.yaml"))
                else:
                    self.assertNotEqual(result.returncode, 0)

    def test_sky_uses_the_context_verified_by_preflight(self):
        for override, context in ((None, "microk8s"), ("bm-local", "bm-local")):
            with self.subTest(context=context):
                result, calls = self.launch("sky", "True 2", override)
                self.assertEqual(result.returncode, 0, result.stderr)
                kube_calls = [call for call in calls if call[0] == "kubectl"]
                self.assertEqual(len(kube_calls), 3)
                self.assertTrue(all(call[1:3] == ["--context", context] for call in kube_calls))
                sky_call = calls[-1]
                self.assertEqual(sky_call[:2], ["sky", "launch"])
                self.assertIn("--infra", sky_call)
                self.assertEqual(sky_call[sky_call.index("--infra") + 1], f"k8s/{context}")
