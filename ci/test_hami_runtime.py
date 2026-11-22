import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


RUNTIME = Path(__file__).resolve().parents[1] / "bootstrap/nebius/management/hami/runtime"
POOL = "airaware.training/pool"
MODE = "airaware.training.node-restriction.kubernetes.io/gpu-mode"
NAMESPACE = "nvidia-gpu-operator"


class HamiRuntimeTests(unittest.TestCase):
    def test_runtime_components_have_a_single_exclusive_driver_profile(self):
        values = yaml.safe_load((RUNTIME / "values.yaml").read_text())
        self.assertFalse(values["devicePlugin"]["enabled"])
        self.assertFalse(values["cdi"]["enabled"])
        self.assertTrue(values["toolkit"]["enabled"])
        self.assertEqual(values["operator"]["runtimeClass"], "nvidia")
        self.assertFalse(values["driver"]["usePrecompiled"])
        self.assertEqual(values["driver"]["kernelModuleType"], "open")
        self.assertEqual(values["driver"]["version"], "580.173.02")
        profiles = values["nebius"]["nvidiaDriverCRDPatch"]["profiles"]
        self.assertEqual(len(profiles), 1)
        self.assertEqual(
            profiles[0]["nodeSelector"],
            {POOL: "training-hami", MODE: "hami", "node.kubernetes.io/instance-type": "gpu-rtx6000-a"},
        )
        worker = values["node-feature-discovery"]["worker"]
        self.assertEqual(worker["nodeSelector"], {POOL: "training-hami", MODE: "hami"})
        for tolerations in (worker["tolerations"], values["daemonsets"]["tolerations"]):
            self.assertIn(
                {"key": "airaware.training/gpu-mode", "operator": "Equal", "value": "hami", "effect": "NoSchedule"},
                tolerations,
            )

    def test_fence_mutates_daemonset_templates_only_in_operator_namespace(self):
        namespace, policy, binding = yaml.safe_load_all((RUNTIME / "daemonset-fence.yaml").read_text())
        self.assertEqual(namespace["metadata"]["name"], NAMESPACE)
        spec = policy["spec"]
        self.assertEqual(spec["failurePolicy"], "Fail")
        self.assertEqual(
            spec["matchConstraints"]["namespaceSelector"]["matchLabels"],
            {"kubernetes.io/metadata.name": NAMESPACE},
        )
        self.assertEqual(
            spec["matchConstraints"]["resourceRules"],
            [{"apiGroups": ["apps"], "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE"],
              "resources": ["daemonsets"], "scope": "Namespaced"}],
        )
        mutation = spec["mutations"][0]["applyConfiguration"]["expression"]
        self.assertIn("nodeSelector:", mutation)
        self.assertIn(f'"{POOL}": "training-hami"', mutation)
        self.assertIn(f'"{MODE}": "hami"', mutation)
        self.assertEqual(binding["spec"]["policyName"], policy["metadata"]["name"])

    def test_render_rejects_an_unverified_archive_and_an_unsupported_minor(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "gpu-operator.tgz"
            artifact.write_bytes(b"wrong")
            for version, error in (("1.36.3", "SHA-256"), ("1.37.0", "Kubernetes 1.36")):
                with self.subTest(version=version):
                    result = subprocess.run(
                        [str(RUNTIME / "render"), str(artifact), version],
                        capture_output=True, text=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(error, result.stderr)

    def test_verifier_requires_the_marker_from_the_scoped_validator(self):
        node = "computeinstance-test"
        cpu = "computeinstance-cpu"
        runtime_labels = {POOL: "training-hami", MODE: "hami"}
        ready = [{"type": "Ready", "status": "True"}]
        nodes = {"items": [
            {
                "metadata": {"name": node, "uid": "gpu-node-uid", "labels": {
                    **runtime_labels,
                    "node.kubernetes.io/instance-type": "gpu-rtx6000-a",
                    "feature.node.kubernetes.io/pci-10de.present": "true",
                }},
                "spec": {"providerID": f"nebius://{node}", "taints": [{
                    "key": "airaware.training/gpu-mode", "value": "hami", "effect": "NoSchedule",
                }]},
                "status": {"conditions": ready, "nodeInfo": {
                    "osImage": "Ubuntu 24.04.5 LTS", "containerRuntimeVersion": "containerd://2.3.3",
                    "kernelVersion": "6.11.0-1016-nvidia",
                }},
            },
            {"metadata": {"name": cpu, "labels": {POOL: "training-cpu"}}, "spec": {},
             "status": {"conditions": ready}},
        ]}
        marker = "/run/nvidia/validations/toolkit-ready"
        validator_name = "nvidia-operator-validator-example"
        validator = {
            "metadata": {"name": validator_name, "namespace": NAMESPACE, "ownerReferences": [
                {"kind": "DaemonSet", "name": "nvidia-operator-validator"},
            ]},
            "spec": {"nodeName": node, "runtimeClassName": "nvidia",
                     "volumes": [{"name": "run-nvidia-validations",
                                  "hostPath": {"path": "/run/nvidia/validations"}}],
                     "containers": [{"name": "nvidia-operator-validator", "image": "validator",
                                     "volumeMounts": [{"name": "run-nvidia-validations",
                                                       "mountPath": "/run/nvidia/validations"}]}]},
            "status": {"conditions": ready},
        }
        controller = {"metadata": {"name": "gpu-operator-example", "labels": {
            "app.kubernetes.io/component": "gpu-operator",
        }}, "spec": {"nodeName": cpu}, "status": {"conditions": ready}}
        daemonsets = {"items": [
            {
                "metadata": {"name": name, "generation": 1, "labels": {"app.kubernetes.io/component": component}},
                "spec": {"template": {"spec": {"nodeSelector": runtime_labels}}},
                "status": {"observedGeneration": 1, "desiredNumberScheduled": 1, "numberReady": 1,
                           "updatedNumberScheduled": 1},
            }
            for name, component in (
                ("nvidia-ubuntu-driver-42", "nvidia-driver"),
                ("nvidia-container-toolkit-daemonset", "toolkit"),
                ("nvidia-operator-validator", "validator"),
                ("gpu-operator-node-feature-discovery-worker", "nfd"),
            )
        ]}
        ca = b"independently enrolled CA"
        responses = {
            "config view": {
                "server": "https://pu.mk8scluster-test.mk8s.eu-south1.nebius.cloud:443",
                "certificate-authority-data": base64.b64encode(ca).decode(),
            },
            "version": {"serverVersion": {"gitVersion": "v1.36.3"}},
            "get nodes": nodes,
            "get pods -A": {"items": [validator]},
            "get clusterpolicy": {"status": {"state": "ready"}, "spec": {
                "devicePlugin": {"enabled": False}, "toolkit": {"enabled": True},
            }},
            "get runtimeclass": {"handler": "nvidia"},
            "get daemonsets": daemonsets,
            "get pods": {"items": [validator, controller]},
        }
        with tempfile.TemporaryDirectory() as directory:
            client = Path(directory) / "kubectl"
            client.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, sys\n"
                "args = ' '.join(sys.argv[1:])\n"
                f"responses = {responses!r}\n"
                "if 'auth whoami' in args:\n"
                "    print('approved-operator')\n"
                "elif ' exec ' in args:\n"
                f"    assert args.endswith('exec {validator_name} -c nvidia-operator-validator -- test -f {marker}')\n"
                "    sys.exit(int(os.environ['MOCK_MARKER_STATUS']))\n"
                "else:\n"
                "    print(json.dumps(next(value for key, value in responses.items() if key in args)))\n"
            )
            client.chmod(0o755)
            environment = dict(
                os.environ,
                PATH=directory + os.pathsep + os.environ["PATH"],
                OPERATOR_CONFIG="operator-kubeconfig", OPERATOR_CONTEXT="prod-nebius",
                NEBIUS_OPERATOR_IDENTITY="approved-operator", NEBIUS_API_ENDPOINT="public",
                NEBIUS_TRAINING_CLUSTER_ID="mk8scluster-test", NEBIUS_CA_BUNDLE_SHA256=hashlib.sha256(ca).hexdigest(),
                HAMI_INSTANCE_ID=node, MOCK_MARKER_STATUS="0",
            )
            cmd = [str(RUNTIME / "manage"), "verify"]
            verified = subprocess.run(cmd, capture_output=True, text=True, env=environment)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            receipt = json.loads(verified.stdout)
            self.assertEqual(receipt["validator"], {"pod": validator_name, "marker": marker})
            self.assertEqual(receipt["node"]["providerID"], f"nebius://{node}")
            environment["MOCK_MARKER_STATUS"] = "1"
            missing = subprocess.run(cmd, capture_output=True, text=True, env=environment)
            self.assertNotEqual(missing.returncode, 0)
            self.assertEqual(missing.stdout, "")

    @unittest.skipUnless(os.environ.get("HAMI_RUNTIME_CHART"), "verified Nebius chart archive supplied separately")
    def test_rendered_chart_keeps_the_driver_and_runtime_scoped(self):
        manifests = subprocess.check_output(
            [str(RUNTIME / "render"), os.environ["HAMI_RUNTIME_CHART"], "v1.36.3"],
            text=True,
        )
        objects = [obj for obj in yaml.safe_load_all(manifests) if obj]
        drivers = [obj["spec"] for obj in objects if obj["kind"] == "NVIDIADriver"]
        self.assertEqual(len(drivers), 1)
        self.assertEqual(drivers[0]["nodeSelector"][MODE], "hami")
        self.assertEqual(drivers[0]["nodeSelector"][POOL], "training-hami")
        policy = next(obj["spec"] for obj in objects if obj["kind"] == "ClusterPolicy")
        self.assertFalse(policy["devicePlugin"]["enabled"])
        self.assertTrue(policy["toolkit"]["enabled"])
        self.assertFalse(policy["cdi"]["enabled"])
        worker = next(obj for obj in objects if obj["kind"] == "DaemonSet")
        self.assertEqual(worker["spec"]["template"]["spec"]["nodeSelector"][MODE], "hami")
        self.assertEqual(worker["spec"]["template"]["spec"]["nodeSelector"][POOL], "training-hami")


if __name__ == "__main__":
    unittest.main()
