"""Check the operator-only tracking stack's placement and trust boundaries."""

from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "bootstrap/nebius/management/tracking"


class TrackingStackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = subprocess.check_output(["kubectl", "kustomize", str(STACK)], text=True)
        cls.objects = {
            (obj["kind"], obj["metadata"]["name"]): obj
            for obj in yaml.safe_load_all(rendered)
        }

    def get(self, kind, name):
        return self.objects[kind, name]

    def test_private_proxy_and_postgres(self):
        proxy = self.get("Service", "mlflow")["spec"]
        self.assertEqual(proxy["type"], "ClusterIP")
        self.assertNotIn("externalIPs", proxy)
        self.assertEqual(proxy["ports"], [{"name": "https", "port": 443, "targetPort": "https"}])
        config = self.get("ConfigMap", "mlflow-runtime")["data"]
        self.assertIn("reverse_proxy 127.0.0.1:5000", config["Caddyfile"])
        self.assertIn("header_up -Authorization", config["Caddyfile"])
        self.assertIn("respond 401", config["Caddyfile"])
        self.assertIn("persist_config off", config["Caddyfile"])
        hba = [line.split() for line in config["pg_hba.conf"].splitlines() if line.strip()]
        self.assertEqual(hba, [
            ["local", "all", "postgres", "peer"],
            ["hostssl", "mlflow", "mlflow", "all", "scram-sha-256"],
            ["hostssl", "mlflow_restore", "mlflow_restore", "all", "scram-sha-256"],
        ])
        self.assertIn("addressing_style = path", config["aws-config"])
        self.assertIn("--serve-artifacts", config["start-mlflow.py"])
        self.assertIn('"127.0.0.1"', config["start-mlflow.py"])

    def test_restricted_cpu_storage_and_secrets(self):
        ns = self.get("Namespace", "tracking")["metadata"]["labels"]
        self.assertEqual(ns["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertEqual(ns["pod-security.kubernetes.io/enforce-version"], "v1.36")
        for key in (("Deployment", "mlflow"), ("StatefulSet", "postgres")):
            workload = self.objects[key]["spec"]
            pod = workload["template"]["spec"]
            self.assertEqual(workload["replicas"], 1)
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["nodeSelector"]["airaware.training/pool"], "training-cpu")
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
            for container in pod["containers"]:
                self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
                self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
                self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
                self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
                for pair in ("requests", "limits"):
                    self.assertEqual(set(container["resources"][pair]), {"cpu", "memory", "ephemeral-storage"})
        postgres = self.get("StatefulSet", "postgres")["spec"]["template"]["spec"]
        self.assertIn({"name": "pg-data", "persistentVolumeClaim": {"claimName": "postgres"}}, postgres["volumes"])
        self.assertNotIn(("PersistentVolumeClaim", "postgres"), self.objects)
        self.assertFalse(any(kind in {"Secret", "ClusterRoleBinding", "RoleBinding"} for kind, _ in self.objects))

    def test_network_is_default_deny_with_exact_artifact_fqdn(self):
        default = self.get("NetworkPolicy", "default-deny")["spec"]
        self.assertEqual(default, {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
        artifact = self.get("CiliumNetworkPolicy", "mlflow-dns-and-artifacts")["spec"]["egress"]
        self.assertEqual(artifact[1]["toFQDNs"], [{"matchName": "storage.eu-south1.nebius.cloud"}])
        self.assertEqual(artifact[1]["toPorts"], [{"ports": [{"port": "443", "protocol": "TCP"}]}])
        policy = self.get("NetworkPolicy", "mlflow-ingress")["spec"]["ingress"]
        self.assertEqual(policy[0]["from"][0]["namespaceSelector"]["matchLabels"],
                         {"kubernetes.io/metadata.name": "training"})
        self.assertEqual(policy[0]["from"][0]["podSelector"]["matchExpressions"], [{
            "key": "airaware.training/phase", "operator": "In", "values": ["tracking-check", "fit", "infer"],
        }])

    def test_database_credentials_and_replicas_are_bounded(self):
        app = self.get("Deployment", "mlflow")["spec"]
        self.assertEqual(app["strategy"], {"type": "Recreate"})
        pod = app["template"]["spec"]
        app_container, proxy = pod["containers"]
        self.assertEqual([c["name"] for c in (app_container, proxy)], ["mlflow", "caddy"])
        app_volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(app_volumes["db-password"]["secret"]["items"],
                         [{"key": "password", "path": "password"}])
        self.assertEqual(app_volumes["db-ca"]["secret"]["items"], [{"key": "ca.crt", "path": "ca.crt"}])
        self.assertEqual({mount["name"] for mount in proxy["volumeMounts"]},
                         {"proxy-runtime", "proxy-secret", "caddy-tmp"})
        self.assertNotIn("proxy-secret", {mount["name"] for mount in app_container["volumeMounts"]})
        env = {entry["name"]: entry for entry in app_container["env"]}
        self.assertEqual(env["PGSSLMODE"]["value"], "verify-full")
        self.assertEqual(env["MLFLOW_S3_ENDPOINT_URL"]["value"], "https://storage.eu-south1.nebius.cloud")
        self.assertEqual(env["MLFLOW_ENABLE_PROXY_MULTIPART_UPLOAD"]["value"], "false")
        self.assertEqual(env["AWS_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"], "mlflow-artifacts")

        postgres = self.get("StatefulSet", "postgres")["spec"]["template"]["spec"]
        postgres_volumes = {volume["name"]: volume for volume in postgres["volumes"]}
        self.assertEqual(postgres["securityContext"]["fsGroup"], 999)
        self.assertEqual(postgres_volumes["db-tls"]["secret"]["defaultMode"], 0o440)
        self.assertIn("ssl=on", postgres["containers"][0]["args"])
        config = self.get("ConfigMap", "mlflow-runtime")["data"]
        self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE", config["init-db.sh"])
