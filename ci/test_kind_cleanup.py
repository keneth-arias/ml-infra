from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

KIND = Path(__file__).resolve().parents[1] / "bootstrap/nebius/management/kind"
sys.path.insert(0, str(KIND))
import render
import tick

IMAGE = "docker.io/local/nebius-cleanup@sha256:" + "a" * 64
NOW = datetime(2026, 11, 21, 10, tzinfo=timezone.utc)


class KindBackup(unittest.TestCase):
    def test_credentials_and_privileges(self):
        namespace, account, role, _, cron = render.resources("pilot", IMAGE, "pilot")
        self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"], "restricted")
        self.assertFalse(account["automountServiceAccountToken"])
        self.assertTrue(all(r["verbs"] == ["get"] and "secrets" not in r["resources"] for r in role["rules"]))
        self.assertTrue(cron["spec"]["suspend"])
        spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        self.assertEqual(spec["containers"][0]["imagePullPolicy"], "Never")
        sources = spec["volumes"][-1]["projected"]["sources"]
        self.assertEqual([s["secret"]["name"] for s in sources if "secret" in s],
                         ["fw13-auth-key", "expiry-pilot-state", "fw13-otlp"])
        rehearsal = render.resources("pilot", IMAGE, "pilot", armed=True, rehearsal=True)[-1]
        self.assertNotIn('"secret"', json.dumps(rehearsal))
        self.assertNotIn('"/scope"', json.dumps(rehearsal))

    def test_controller_proof_and_rejections(self):
        cron = render.resources("demo", IMAGE, "pilot", armed=True)[-1]
        cron["metadata"]["uid"] = "cron-uid"
        cron["status"] = {"lastScheduleTime": tick.stamp(NOW)}
        job = deepcopy(cron["spec"]["jobTemplate"])
        job["metadata"] = {"uid": "job-uid", "annotations": {
            "batch.kubernetes.io/cronjob-scheduled-timestamp": NOW.isoformat()},
            "ownerReferences": [{"kind": "CronJob", "controller": True, "uid": "cron-uid", "name": "expiry-demo"}]}
        pod = deepcopy(job["spec"]["template"])
        pod["metadata"] = {"uid": "pod-uid", "ownerReferences": [
            {"kind": "Job", "controller": True, "uid": "job-uid", "name": "scheduled-job"}]}
        objects = {"expiry-demo": cron, "scheduled-job": job, "scheduled-pod": pod}
        def read():
            return tick.scheduler("demo", get=lambda group, resource, name: objects[name])
        with patch.dict(tick.os.environ, POD_NAME="scheduled-pod", POD_UID="pod-uid"):
            proof = read()
            self.assertEqual(tick.check_tick(proof, proof, NOW), NOW + timedelta(minutes=1))
            with self.assertRaises(ValueError):
                tick.check_tick(proof, proof, NOW + timedelta(minutes=1))
            with self.assertRaises(ValueError):
                tick.check_tick(proof, {**proof, "spec_sha256": "0"*64}, NOW)
            for obj, changes in ((cron["spec"], {"suspend": True}), (job["metadata"], {"uid": "replaced"}),
                                 (cron["metadata"], {"deletionTimestamp": tick.stamp(NOW)}),
                                 (pod["spec"]["containers"][0], {"args": ["demo", "gpu"]})):
                with self.subTest(changes=changes), patch.dict(obj, changes), self.assertRaises(ValueError):
                    read()

    def test_cutoff_remains_effective_after_preallocation_proofs_expire(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            inventory = json.dumps({"project_id": "project", "cluster_id": "cluster"}).encode()
            (path / "inventory.json").write_bytes(inventory)
            scope = dict(run_id="r", stage="pilot", project_id="project", cluster_id="cluster",
                         cutoff=tick.stamp(NOW), fw13_attest_until=tick.stamp(NOW-timedelta(hours=1)),
                         fw13_public_key_id="key", baseline_sha256=hashlib.sha256(inventory).hexdigest())
            (path / "scope.json").write_text(json.dumps(scope))
            with patch.object(tick, "initialize", return_value=17) as init, patch.object(tick, "scheduler") as scheduler, \
                 patch.object(tick.subprocess, "run") as release, patch.object(tick, "utc", return_value=NOW-timedelta(seconds=1)):
                tick.run_tick("r", "pilot", False, scope_dir=path)
                init.assert_not_called()
                with patch.object(tick, "utc", return_value=NOW):
                    tick.run_tick("r", "pilot", False, scope_dir=path)
                release.assert_called_once_with([str(tick.ROOT/"expire-workers"), "--apply", "--run-id", "r",
                    "--cutoff", scope["cutoff"], "--project-id", "project", "--cluster-id", "cluster",
                    "--inventory", str(path/"inventory.json")], check=True)
                scheduler.assert_not_called()
                release.reset_mock()
                (path/"inventory.json").write_bytes(inventory + b" ")
                with self.assertRaises(ValueError):
                    tick.run_tick("r", "pilot", False, scope_dir=path)
                release.assert_not_called()

    def test_shutdown_unlocks_and_fences_future_terraform_commands(self):
        import subprocess
        import time
        with TemporaryDirectory() as folder:
            control = Path(folder)
            lock = control / "state.lock"
            # A second process can acquire the simulated S3 lock only after the
            # first CLI completes its interrupt handler and removes the object.
            fake_cli = """
import os, signal, sys, time
from pathlib import Path
control = Path(sys.argv[1])
def interrupt(*_):
    (control/'state.lock').unlink()
    sys.exit(130)
signal.signal(signal.SIGINT, interrupt)
(control/'state.lock').touch(exist_ok=False)
(control/'terraform.pid').write_text(str(os.getpid()))
time.sleep(30)
"""
            child = subprocess.Popen([sys.executable, "-c", fake_cli, folder], start_new_session=True)
            try:
                until = time.monotonic() + 5
                while not (control/"terraform.pid").exists() and time.monotonic() < until:
                    time.sleep(0.01)
                self.assertTrue(lock.exists())
                tick.stop_worker(child, control, grace=3, terraform_binary=sys.executable)
                self.assertEqual(child.wait(timeout=1), 130)
                lock.touch(exist_ok=False)
                result = subprocess.run([str(KIND/"bin/terraform"), "apply"],
                                        env={**tick.os.environ, "CLEANUP_CONTROL": folder})
                self.assertEqual(result.returncode, 130)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_supervisor_handles_deadline_and_pod_termination(self):
        import signal
        from types import SimpleNamespace
        for expire in (True, False):
            # Deliver the kubelet signal to our PID while supervise owns its handler.
            def start(*args, **kwargs):
                if not expire:
                    tick.os.kill(tick.os.getpid(), signal.SIGTERM)
                return SimpleNamespace(poll=lambda: None)
            with self.subTest(deadline=expire), patch.object(tick.subprocess, "Popen", side_effect=start), \
                 patch.object(tick, "stop_worker") as stop, self.assertRaisesRegex(RuntimeError, "interrupted"):
                try:
                    tick.supervise(["worker"], timeout=0 if expire else 10)
                finally:
                    stop.assert_called_once()
