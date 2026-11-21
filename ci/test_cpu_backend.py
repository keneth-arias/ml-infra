import hashlib
from pathlib import Path
import runpy
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
MGMT = ROOT / "bootstrap/nebius/management"
BACKEND = runpy.run_path(str(MGMT / "check-backend.py"))


class CpuBackendTest(unittest.TestCase):
    def test_only_qualified_server_can_start(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.yaml"
            qualified = yaml.safe_load((MGMT / "config.yaml").read_bytes())
            self.assertEqual(
                qualified,
                {
                    "allowed_clouds": ["kubernetes"],
                    "kubernetes": {
                        "allowed_contexts": ["prod-nebius"],
                        "namespace": "sky-cpu",
                        "remote_identity": "NONE",
                        "context_configs": {"prod-nebius": {"ports": "podip"}},
                    },
                },
            )
            env = {
                "SKYPILOT_CONFIG": str(config),
                "KUBECONFIG": "scoped",
                "NEBIUS_SUBMIT_KUBECONFIG": "scoped",
            }
            check = BACKEND["check_config"]
            config.write_text(yaml.safe_dump(qualified))
            check(env, ["prod-nebius"], "0.14.3")
            for change, contexts, version in (
                (
                    {"SKYPILOT_ALLOW_ALL_KUBERNETES_CONTEXTS": ""},
                    ["prod-nebius"],
                    "0.14.3",
                ),
                (
                    {"SKYPILOT_API_SERVER_ENDPOINT": "elsewhere"},
                    ["prod-nebius"],
                    "0.14.3",
                ),
                ({"KUBECONFIG": "another"}, ["prod-nebius"], "0.14.3"),
                ({}, ["prod-nebius", "dev-bm"], "0.14.3"),
                ({}, ["prod-nebius"], "0.15.0"),
            ):
                with (
                    self.subTest(change=change, contexts=contexts, version=version),
                    self.assertRaises(ValueError),
                ):
                    check(env | change, contexts, version)
            qualified["kubernetes"]["namespace"] = "training"
            config.write_text(yaml.safe_dump(qualified))
            with self.assertRaises(ValueError):
                check(env, ["prod-nebius"], "0.14.3")

    def test_offline_wheel_names_and_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheels = root / "wheels"
            wheels.mkdir()
            wheel = wheels / "demo-1.0-py3-none-any.whl"
            wheel.write_bytes(b"fixture")
            lock = root / "build-wheels.txt"
            lock.write_text(
                "demo==1.0 --hash=sha256:"
                + hashlib.sha256(b"fixture").hexdigest()
                + "\n"
            )
            env = {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": str(wheels)}
            check = BACKEND["check_wheels"]
            check(env, lock)
            with self.assertRaises(ValueError):
                check(env | {"PIP_NO_INDEX": "0"}, lock)
            wheel.rename(wheels / "different-2.0-py3-none-any.whl")
            with self.assertRaises(ValueError):
                check(env, lock)
            (wheels / "different-2.0-py3-none-any.whl").rename(root / "source")
            wheel.symlink_to(root / "source")
            with self.assertRaises(ValueError):
                check(env, lock)
            wheel.unlink()
            wheel.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                check(env, lock)
