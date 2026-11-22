import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAUNCH = Path(__file__).resolve().parents[1] / "workloads/sky/launch-episode.sh"


class EpisodeLaunchTest(unittest.TestCase):
    def test_native_launch_checks_and_uses_the_local_context(self):
        for resources, submits in (("True 2", True), ("False 2", False), ("True 0", False)):
            with self.subTest(resources=resources), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                log = root / "calls.jsonl"
                kubectl = root / "kubectl"
                kubectl.write_text(f"""#!{sys.executable}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["KUBE_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "get" in args:
    print(os.environ["BM_RESOURCES"])
elif "create" in args:
    print("job/preflight")
""")
                kubectl.chmod(0o755)
                env = {
                    **os.environ,
                    "PATH": f"{root}:{os.environ['PATH']}",
                    "KUBECONFIG": str(root / "user-config-with-nebius-default"),
                    "KUBE_CALLS": str(log),
                    "BM_RESOURCES": resources,
                }
                env.pop("EPISODE_LOCAL_CONTEXT", None)
                result = subprocess.run(
                    [
                        LAUNCH, "job", "infer", "direct", "station-trial",
                        "--image", "registry/test@sha256:" + "a" * 64,
                        "--bundle-sha256", "b" * 64, "--step", "14838",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                self.assertTrue(all(call[:2] == ["--context", "microk8s"] for call in calls))
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
