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

    def test_allocated_worker_returns_without_publishing_a_funding_proof(self):
        with TemporaryDirectory() as folder:
            path = Path(folder)
            baseline = dict(project_id="project", cluster_id="cluster",
                            group_ids=[], instance_ids=[], disk_ids=[])
            raw = json.dumps(baseline).encode()
            (path / "inventory.json").write_bytes(raw)
            scope = dict(
                run_id="demo", stage="pilot", project_id="project", cluster_id="cluster",
                state_bucket="state", control_bucket="control", control_project_id="management",
                cutoff=tick.stamp(NOW + timedelta(hours=1)),
                fw13_attest_until=tick.stamp(NOW + timedelta(minutes=3)), fw13_public_key_id="key",
                baseline_sha256=hashlib.sha256(raw).hexdigest(),
            )
            (path / "scope.json").write_text(json.dumps(scope))
            with patch.object(tick, "initialize", return_value=17), \
                 patch.object(tick, "utc", return_value=NOW), \
                 patch.object(tick.Path, "read_text", autospec=True) as read, \
                 patch.object(tick.runpy, "run_path", return_value={"send": lambda *_, **__: None}), \
                 patch.object(tick, "plan_release", return_value=({"group"}, set())), \
                 patch.object(tick, "verify_inventory") as preallocation, \
                 patch.object(tick, "verify_allocated_inventory") as allocated, \
                 patch.object(tick, "publish_timer_proof") as publish:
                read.side_effect = lambda file: json.dumps(scope) if file.name == "scope.json" else "bearer"
                tick.run_tick("demo", "pilot", False, scope_dir=path)
                allocated.assert_called_once_with("ml-prod", scope, baseline, {"group"})
                preallocation.assert_not_called()
                publish.assert_not_called()

    def test_allocated_worker_inventory_requires_exact_links_and_healthy_worker(self):
        labels = {"mk8s-cluster-id": "cluster", "mk8s-node-group-id": "cpu"}
        scope = dict(stage="pilot", project_id="project", cluster_id="cluster", run_id="demo",
                     cutoff=tick.stamp(NOW+timedelta(hours=1)))
        group_labels = {"airaware-run": "demo", "airaware-cutoff": "20261121110000"}
        baseline = dict(project_id="project", cluster_id="cluster", group_ids=[], instance_ids=[], disk_ids=[])
        inventory = {
            "node-group": {"cpu": {"metadata": {"parent_id": "cluster", "labels": group_labels},
                                   "status": {"state": "RUNNING", "ready_node_count": "1",
                                              "node_count": "1", "target_node_count": "1"}}},
            "instance": {"vm": {"metadata": {"labels": labels}, "status": {"state": "RUNNING", "disk_attachments": [
                                    {"name": "boot", "id": "disk", "is_managed": True}]},
                                "spec": {"resources": {"platform": "cpu-d3", "preset": "4vcpu-16gb"},
                                         "boot_disk": {"managed_disk": {"name": "boot", "labels": labels}}}}},
            "disk": {"disk": {"metadata": {"name": "boot", "labels": labels},
                              "status": {"state": "READY", "read_write_attachment": "vm"}}},
        }
        with patch.object(tick, "cloud_items", side_effect=lambda _, service, kind, parent: inventory[kind]):
            tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})
            for kind in ("node-group", "instance", "disk"):
                with self.subTest(missing_kind=kind), patch.dict(inventory, {kind: {}}):
                    with self.assertRaises(ValueError):
                        tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})
            with patch.dict(inventory["instance"]["vm"]["status"], state="STOPPED"), self.assertRaises(ValueError):
                tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})
            with patch.dict(inventory["disk"]["disk"]["metadata"], name="other"), self.assertRaises(ValueError):
                tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})
            with patch.dict(inventory["disk"], unrelated={"metadata": {}}), self.assertRaises(ValueError):
                tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})
            with self.assertRaises(ValueError):
                tick.verify_allocated_inventory("ml-prod", scope, baseline, {"different-group"})

            with patch.dict(inventory["node-group"]["cpu"]["metadata"]["labels"], **{"airaware-run": "foreign"}):
                with self.assertRaises(ValueError):
                    tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})

            vm_status = inventory["instance"]["vm"]["status"]
            disk_status = inventory["disk"]["disk"]["status"]
            for status, changes in (
                (vm_status, {"disk_attachments": []}),
                (vm_status, {"disk_attachments": [{"name": "boot", "id": ""}]}),
                (vm_status, {"disk_attachments": [{"name": "boot", "id": "wrong", "is_managed": True}]}),
                (vm_status, {"reconciling": True}),
                (disk_status, {"state": "BROKEN"}),
                (disk_status, {"read_write_attachment": "wrong"}),
                (disk_status, {"reconciling": True}),
            ):
                with self.subTest(changes=changes), patch.dict(status, changes), self.assertRaises(ValueError):
                    tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})

            retained = {"name": "postgres-csi", "id": "approved-csi", "is_managed": False}
            with patch.dict(inventory["disk"], **{"approved-csi": {"metadata": {"name": "postgres-csi"}}}), \
                 patch.dict(baseline, disk_ids=["approved-csi"]), \
                 patch.dict(vm_status, disk_attachments=[*vm_status["disk_attachments"], retained]):
                tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})
                with patch.dict(retained, id="unapproved-csi"), self.assertRaises(ValueError):
                    tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu"})

            gpu_group = deepcopy(inventory["node-group"]["cpu"])
            gpu_vm = deepcopy(inventory["instance"]["vm"])
            gpu_disk = deepcopy(inventory["disk"]["disk"])
            gpu_vm["spec"]["resources"] = {"platform": "gpu-rtx6000-a", "preset": "1gpu-24vcpu-218gb"}
            gpu_vm["spec"]["boot_disk"]["managed_disk"]["name"] = gpu_disk["metadata"]["name"] = "gpu-boot"
            gpu_vm["status"]["disk_attachments"] = [{"name": "gpu-boot", "id": "gpu-disk", "is_managed": True}]
            gpu_disk["status"]["read_write_attachment"] = "gpu-vm"
            gpu_labels = {**labels, "mk8s-node-group-id": "gpu"}
            for record in (gpu_vm["metadata"], gpu_disk["metadata"], gpu_vm["spec"]["boot_disk"]["managed_disk"]):
                record["labels"] = gpu_labels
            inventory["node-group"]["gpu"] = gpu_group
            inventory["instance"]["gpu-vm"] = gpu_vm
            inventory["disk"]["gpu-disk"] = gpu_disk
            scope.update(stage="gpu", support_group_id="cpu", support_instance_id="vm", support_boot_disk_id="disk")
            tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu", "gpu"})
            with patch.dict(scope, support_instance_id="gpu-vm"), self.assertRaises(ValueError):
                tick.verify_allocated_inventory("ml-prod", scope, baseline, {"cpu", "gpu"})

    def test_locked_rehearsal_uses_group_inventory_after_allocate_commits(self):
        import worker_cleanup
        addr = 'nebius_mk8s_v1_node_group.training["cpu"]'
        labels = {"airaware-run": "demo", "airaware-cutoff": "20261121110000"}
        worker = dict(id="cpu", parent_id="cluster", labels=labels)
        change = dict(address=addr, mode="managed", change=dict(actions=["delete"], before=worker))
        plan = dict(format_version="1.2", complete=False, errored=False, applyable=True,
                    resource_changes=[change])
        live = {}

        def terraform(action, *args, **kwargs):
            if action == "plan":
                # The funding apply finishes while the rehearsal waits for its lock.
                live["cpu"] = {"metadata": worker}
                return None
            return json.dumps(plan).encode()

        with patch.object(worker_cleanup, "terraform", side_effect=terraform), \
             patch.object(worker_cleanup, "cloud_items", side_effect=lambda *args: dict(live)):
            self.assertEqual(worker_cleanup.plan_release(
                "rehearsal", profile="ml-prod", cluster_id="cluster", run_id="demo",
                cutoff_label="20261121110000"), ({"cpu"}, set()))

    def test_cutoff_remains_effective_without_control_storage_after_proofs_expire(self):
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
                # A new proof requires the split, even on the same persisted legacy scope.
                scope["fw13_attest_until"] = scope["cutoff"]
                (path / "scope.json").write_text(json.dumps(scope))
                with self.assertRaisesRegex(ValueError, "control_bucket"):
                    tick.run_tick("r", "pilot", False, scope_dir=path)
                release.assert_not_called()
                (path/"inventory.json").write_bytes(inventory + b" ")
                with self.assertRaises(ValueError):
                    tick.run_tick("r", "pilot", False, scope_dir=path)
                release.assert_not_called()

    def test_shutdown_unlocks_and_fences_future_terraform_commands(self):
        import signal
        import subprocess
        import time

        # Portable Python builds may omit the Linux pidfd bindings. Use the
        # distribution interpreter for this Linux integration test in that case.
        if not (hasattr(tick.os, "pidfd_open") and hasattr(signal, "pidfd_send_signal")):
            distro = Path("/usr/bin/python3")
            self.assertFalse(distro.samefile(sys.executable), "The test host needs Linux pidfd support")
            subprocess.run([str(distro), "-B", str(Path(__file__).resolve()),
                            "KindBackup.test_shutdown_unlocks_and_fences_future_terraform_commands"], check=True)
            return
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


if __name__ == "__main__":
    unittest.main()
