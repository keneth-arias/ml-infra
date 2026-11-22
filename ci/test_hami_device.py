import runpy
import subprocess
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
HAMi = ROOT / "platform/hami"
DEVICE = HAMi / "device"
SELECTOR = {
    "gpu": "on",
    "airaware.training/gpu-mode": "hami",
    "airaware.training/pool": "training-hami",
    "airaware.training.node-restriction.kubernetes.io/gpu-mode": "hami",
}


def build(path):
    rendered = subprocess.check_output(["kubectl", "kustomize", str(path)], text=True)
    return {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered) if obj}


class HamiDeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = build(DEVICE)
        cls.operator = build(DEVICE / "operator")
        cls.system = build(HAMi / "system")

    def test_device_flux_contains_only_isolated_named_objects(self):
        self.assertEqual(set(self.core), {
            ("ServiceAccount", "hami-device-plugin"),
            ("ConfigMap", "hami-device-plugin"),
            ("ConfigMap", "hami-scheduler-device"),
            ("Service", "hami-device-plugin-monitor"),
            ("DaemonSet", "hami-device-plugin"),
        })
        for obj in self.core.values():
            self.assertEqual(obj["metadata"]["namespace"], "hami-device")
        self.assertEqual(
            self.core["ConfigMap", "hami-scheduler-device"]["data"],
            self.system["ConfigMap", "hami-scheduler-device"]["data"],
        )
        plugin = self.core["DaemonSet", "hami-device-plugin"]["spec"]["template"]["spec"]
        self.assertEqual(plugin["nodeSelector"], SELECTOR)
        self.assertEqual(plugin["serviceAccountName"], "hami-device-plugin")
        self.assertEqual(plugin["runtimeClassName"], "nvidia")
        self.assertFalse(plugin["hostNetwork"])
        self.assertIn({"key": "airaware.training/gpu-mode", "value": "hami", "operator": "Equal",
                       "effect": "NoSchedule"}, plugin["tolerations"])

    def test_operator_driver_and_toolkit_are_explicit(self):
        plugin = self.core["DaemonSet", "hami-device-plugin"]["spec"]["template"]["spec"]
        volumes = {volume["name"]: volume for volume in plugin["volumes"]}
        self.assertEqual(volumes["driver-root"]["hostPath"], {"path": "/run/nvidia/driver", "type": "Directory"})
        self.assertEqual(volumes["nvidia-validations"]["hostPath"]["path"], "/run/nvidia/validations")
        self.assertFalse(any(volume.get("hostPath", {}).get("path") == "/" for volume in volumes.values()))
        validator, = plugin["initContainers"]
        self.assertEqual(validator["name"], "toolkit-validation")
        self.assertEqual(validator["image"], plugin["containers"][0]["image"])
        self.assertIn("/run/nvidia/validations/toolkit-ready", validator["args"][0])
        for container in plugin["containers"]:
            self.assertEqual(next(env["value"] for env in container["env"] if env["name"] == "NVIDIA_DRIVER_ROOT"),
                             "/run/nvidia/driver")
            self.assertIn("/driver-root", [mount["mountPath"] for mount in container["volumeMounts"]
                                           if mount["name"] == "driver-root" and mount["readOnly"]])

    def test_template_policies_match_the_committed_device_core(self):
        renderer = runpy.run_path(str(DEVICE / "render.py"))
        core = yaml.safe_load_all((DEVICE / "core.yaml").read_text())
        daemonset, = (obj for obj in core if obj["kind"] == "DaemonSet")
        self.assertEqual(renderer["render_guard"](daemonset), (DEVICE / "operator/template-admission.yaml").read_text())

    def test_standby_retains_its_cpu_scheduler_and_baseline_namespace(self):
        baseline = yaml.safe_load((HAMi / "system-namespace.yaml").read_text())
        self.assertEqual(baseline["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "baseline")
        scheduler = self.system["Deployment", "hami-scheduler"]["spec"]["template"]["spec"]
        self.assertEqual(scheduler["nodeSelector"], {"airaware.training/pool": "training-cpu"})
        self.assertNotIn(("DaemonSet", "hami-device-plugin"), self.operator)
        self.assertEqual(self.operator["Namespace", "hami-device"]["metadata"]["labels"][
            "pod-security.kubernetes.io/enforce"], "privileged")

    def test_runtime_can_patch_only_fractional_pods_and_constrained_nodes(self):
        cluster = self.operator["ClusterRole", "hami-device-runtime"]
        self.assertEqual({(tuple(rule["resources"]), tuple(rule["verbs"])) for rule in cluster["rules"]}, {
            (("nodes",), ("get", "patch")),
            (("pods",), ("list", "watch")),
        })
        role = self.operator["Role", "hami-device-allocator"]
        self.assertEqual(role["metadata"]["namespace"], "hami-workloads")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "patch"]}])
        for (kind, _), obj in self.operator.items():
            if kind in {"ClusterRoleBinding", "RoleBinding"}:
                self.assertEqual(obj["subjects"], [
                    {"kind": "ServiceAccount", "name": "hami-device-plugin", "namespace": "hami-device"},
                ])
        for name in ("hami-device-pods", "hami-device-daemonset", "hami-device-binding", "hami-device-node-write",
                     "hami-device-allocation-write"):
            policy = self.operator["ValidatingAdmissionPolicy", name]["spec"]
            binding = self.operator["ValidatingAdmissionPolicyBinding", name]["spec"]
            self.assertEqual(policy["failurePolicy"], "Fail")
            self.assertEqual(binding, {"policyName": name, "validationActions": ["Deny"]})
        for name in ("hami-device-node-write", "hami-device-allocation-write"):
            policy = self.operator["ValidatingAdmissionPolicy", name]["spec"]
            self.assertIn("'authentication.kubernetes.io/node-name'",
                          [variable["expression"] for variable in policy["variables"]])


if __name__ == "__main__":
    unittest.main()
