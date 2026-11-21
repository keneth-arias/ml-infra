import importlib.util
from pathlib import Path
import unittest

source = Path(__file__).with_name("render-flux.py")
spec = importlib.util.spec_from_file_location("hami_flux", source)
flux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flux)

COMMIT = "6" * 40


class FluxHandoffTests(unittest.TestCase):
    def test_fetch_only_switches_source(self):
        source, stage = flux.sync(COMMIT, False)
        fetched, staged = flux.sync(COMMIT, True)
        self.assertTrue(source["spec"]["suspend"])
        self.assertFalse(fetched["spec"]["suspend"])
        self.assertEqual(source["spec"]["ref"], {"commit": COMMIT})
        self.assertEqual(stage, staged)
        self.assertTrue(stage["spec"]["suspend"])
        self.assertFalse(stage["spec"]["prune"])
        self.assertEqual(stage["spec"]["deletionPolicy"], "Orphan")
        self.assertEqual(stage["spec"]["serviceAccountName"], "nebius-hami")
        self.assertEqual(stage["spec"]["sourceRef"]["name"], source["metadata"]["name"])
        self.assertEqual(stage["spec"]["path"], "./platform/hami/system")
        self.assertNotIn("decryption", stage["spec"])

    def test_additive_controller_access_and_named_workload_patch(self):
        identity = {"apiVersion": "apps/v1", "kind": "Deployment",
                    "metadata": {"name": "hami-scheduler", "namespace": "hami-system"}}
        identity2 = {"apiVersion": "v1", "kind": "Service",
                     "metadata": {"name": "hami-scheduler", "namespace": "hami-system"}}
        sa, control, control_binding, workload, workload_binding = flux.access([identity, identity2])
        self.assertEqual((sa["metadata"]["namespace"], sa["metadata"]["name"]), ("flux-system", "nebius-hami"))
        self.assertFalse(sa["automountServiceAccountToken"])
        self.assertEqual(control_binding["subjects"], [
            {"kind": "ServiceAccount", "name": "kustomize-controller", "namespace": "flux-system"}])
        self.assertEqual(workload_binding["subjects"], [
            {"kind": "ServiceAccount", "name": "nebius-hami", "namespace": "flux-system"}])
        self.assertTrue(all(r["resourceNames"] == ["nebius-hami"] for r in control["rules"]))
        self.assertTrue(all(r["verbs"] == ["get", "patch"] for r in workload["rules"]))
        self.assertEqual({(r["apiGroups"][0], r["resources"][0], r["resourceNames"][0])
                          for r in workload["rules"]},
                         {("", "services", "hami-scheduler"), ("apps", "deployments", "hami-scheduler")})

    def test_access_rejects_external_privileged_secrets_and_duplicates(self):
        for kind, api, namespace in (("Secret", "v1", "hami-system"),
                                     ("Role", "rbac.authorization.k8s.io/v1", "hami-system"),
                                     ("Deployment", "apps/v1", "training"),
                                     ("ClusterRoleBinding", "rbac.authorization.k8s.io/v1", "hami-system")):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                flux.access([{"kind": kind, "apiVersion": api,
                              "metadata": {"namespace": namespace, "name": "fixture"}}])
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment",
                      "metadata": {"name": "hami-scheduler", "namespace": "hami-system"}}
        with self.assertRaises(ValueError):
            flux.access([deployment, deployment])
        with self.assertRaises(ValueError):
            flux.access([])

    def test_bootstrap_seeds_only_zero_scheduler_replicas(self):
        scheduler = {"apiVersion": "apps/v1", "kind": "Deployment",
                     "metadata": {"name": "hami-scheduler", "namespace": "hami-system"},
                     "spec": {"replicas": 1}}
        plugin = {"apiVersion": "apps/v1", "kind": "DaemonSet",
                  "metadata": {"name": "hami-device-plugin", "namespace": "hami-system"}}
        bootstrap = flux.bootstrap_core([scheduler, plugin])
        self.assertEqual(bootstrap[0]["spec"]["replicas"], 0)
        self.assertEqual(scheduler["spec"]["replicas"], 1)
        self.assertEqual(bootstrap[1], plugin)
        with self.assertRaises(ValueError):
            flux.bootstrap_core([plugin])

    def test_activation_requires_observed_ready_pinned_source_and_suspended_stage(self):
        source, staged = flux.sync(COMMIT, True)
        source["metadata"].update(generation=1, uid="read-back")
        source["status"] = {"observedGeneration": 1, "artifact": {"revision": "HEAD@sha1:" + COMMIT},
                            "conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}]}
        staged["metadata"].update(resourceVersion="492", uid="staged")
        active = flux.activate(COMMIT, source, staged)
        self.assertEqual(active["metadata"]["resourceVersion"], "492")
        self.assertFalse(active["spec"]["suspend"])
        self.assertTrue(staged["spec"]["suspend"])
        source["status"]["observedGeneration"] = 0
        with self.assertRaises(ValueError):
            flux.activate(COMMIT, source, staged)
        source["status"]["observedGeneration"] = 1
        staged["spec"]["path"] = "./clusters/prod-nebius"
        with self.assertRaises(ValueError):
            flux.activate(COMMIT, source, staged)


if __name__ == "__main__":
    unittest.main()
