import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SKY = Path(__file__).resolve().parents[1] / "workloads" / "sky"
sys.path.insert(0, str(SKY))
spec = importlib.util.spec_from_file_location("submit_public", SKY / "submit-public-flamingo-stage.py")
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)


class PublicStageSubmission(unittest.TestCase):
    def test_packet_requires_approved_bytes_and_reproduced_render(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_bytes(b'{"run":"example"}')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            for pin, reproduced in [(digest, {"run": "different"}), ("0" * 64, {"run": "example"})]:
                with self.subTest(pin=pin), self.assertRaises(ValueError):
                    submit.approved_packet(path, pin, reproduced)
            self.assertEqual(submit.approved_packet(path, digest, {"run": "example"}), {"run": "example"})

    def test_activation_requires_successor_of_original_exclusive_handoff(self):
        guard = {"policy_uid": "policy", "binding_uid": "binding", "binding_generation": 1,
                 "policy_generation": 4}
        enrollment = dict(action="enroll", purpose="public-flamingo-stage", run="r",
                          packet_file_sha256="pin", deployment_sha256="deployment", inventory_secret="secret",
                          inventory_uid="inventory", staging_operator="stage", storage_operator="storage",
                          exclusive_storage_handoff=True, inventory_secret_immutable=True,
                          expires_at="2026-11-22T01:10:00Z", closed_guard=guard)
        active = dict(enrollment, action="activate", stage_guard=dict(guard, policy_generation=5, job_uid="job", expires_at=enrollment["expires_at"]))
        submit.check_transition(enrollment, active)
        for override in ({"window_sha256": "b" * 64}, {"storage_operator": "other"}, {"expires_at": "2026-11-22T02:10:00Z"},
                         {"stage_guard": dict(active["stage_guard"], policy_generation=6)}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                submit.check_transition(enrollment, dict(active, **override))

    def test_approval_cannot_reuse_predecessor_window_or_scope(self):
        from types import SimpleNamespace
        window = SimpleNamespace(sha256="a"*64, start="2026-11-22T08:00:00Z", cutoff="2026-11-22T09:00:00Z")
        ref = {"window_sha256": window.sha256}
        approved = {**ref, "stage_guard": ref, "expires_at": window.cutoff}
        submit.check_window_approval(window, approved)
        for patch_data in ({"window_sha256": "0"*64}, {"stage_guard": {}}):
            with self.subTest(patch=patch_data), self.assertRaises(ValueError):
                submit.check_window_approval(window, {**approved, **patch_data})
        with self.assertRaises(ValueError):
            submit.check_window_approval(None, approved)
        with tempfile.TemporaryDirectory() as directory:
            scope = Path(directory) / "scope.json"
            scope.write_text('{"cutoff":"first"}')
            scope.chmod(0o600)
            window.scope_sha256 = hashlib.sha256(scope.read_bytes()).hexdigest()
            self.assertEqual(submit.stage_scope(scope, window, private=True), {"cutoff": "first"})
            scope.write_text('{"cutoff":"changed"}')
            with self.assertRaises(ValueError):
                submit.stage_scope(scope, window, private=True)

    def test_placement_rejects_replacement_and_unschedulable_node(self):
        placement = {"name": "cpu-node", "provider_id": "nebius://cpu-node", "uid": "original"}
        node = {"metadata": {"name": "cpu-node", "uid": "original", "labels": {"airaware.training/pool": "training-cpu"}},
                "spec": {"providerID": placement["provider_id"]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"cpu": "4"}}}
        submit.check_placement(placement, node)
        node["spec"]["unschedulable"] = True
        with self.assertRaises(ValueError):
            submit.check_placement(placement, node)
        node["spec"].pop("unschedulable")
        node["metadata"]["uid"] = "replacement"
        with self.assertRaises(ValueError):
            submit.check_placement(placement, node)

    def test_create_returns_uid_and_never_discloses_admission_diagnostics(self):
        doc = {"kind": "Job", "metadata": {"name": "stage", "namespace": "training"}}
        with patch.object(submit.subprocess, "run") as call:
            call.return_value.returncode = 1
            call.return_value.stderr = "private diagnostic"
            with self.assertRaisesRegex(RuntimeError, "inspect") as error:
                submit.create(["kubectl"], doc)
            self.assertNotIn("private diagnostic", str(error.exception))
            call.return_value.returncode = 0
            call.return_value.stdout = json.dumps({**doc, "metadata": {**doc["metadata"], "uid": "issued"}})
            self.assertEqual(submit.create(["kubectl"], doc), "issued")


class SignedWindowActivation(unittest.TestCase):
    """Drive both activate CLIs with a pinned packet and no external processes."""

    def test_stage_and_owner_use_same_window_for_every_render_and_guard(self):
        for owner_phase in (False, True):
            with self.subTest(owner=owner_phase), tempfile.TemporaryDirectory() as temp:
                self.activate(Path(temp), owner_phase)

    def activate(self, directory, owner_phase):
        from contextlib import ExitStack, redirect_stdout
        from datetime import datetime, timedelta, timezone
        from io import StringIO
        import os
        import runpy
        from types import SimpleNamespace
        from unittest.mock import Mock
        from uuid import uuid4
        import private_api
        import production_target
        import public_flamingo_stage_guard as guards
        import public_flamingo_window as windows

        def save(name, obj):
            path = directory / name
            path.write_text(json.dumps(obj))
            path.chmod(0o600)
            return hashlib.sha256(path.read_bytes()).hexdigest()

        now = datetime.now(timezone.utc)
        end = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        scope_sha = save("scope", {"project_id": "project", "cluster_id": "cluster", "cutoff": end})
        window = SimpleNamespace(sha256="a"*64, scope_sha256=scope_sha, run="fsw-" + "a"*32,
                                 namespace="training", start=(now-timedelta(hours=1)).isoformat(), cutoff=end,
                                 validate_namespace=Mock(), validate_placement=Mock())
        ids = {k: str(uuid4()) for k in ("job", "configmap", "inventory")}
        program = {"metadata": {"name": "program", "annotations": {"airaware.training/stage-window-sha256": window.sha256}},
                   "data": {"worker": "exact"}, "immutable": True}
        owner = runpy.run_path(str(SKY / "prepare-public-flamingo-leaves.py"))
        render = runpy.run_path(str(SKY / "render-public-flamingo-stage.py"))
        # Review metadata is bound by the immutable packet; this offline fixture
        # exercises the management entry point while release verification is isolated.
        job = render["stage_job"]("fixture", "inventory", "program", "signed-image", "w", "o")
        job["metadata"]["annotations"] = program["metadata"]["annotations"]
        packet = {"run": window.run, "window_sha256": window.sha256, "job": job, "configmap": program,
                  "cluster_id": "cluster", "project_id": "project", "binding_sha256": "binding", "placement": {"name": "cpu"}}
        owner_job = owner["owner_job"](job, owner["worker_source"]())
        checked_job = owner_job if owner_phase else job
        name = checked_job["metadata"]["name"]
        cm = {**program, "metadata": {**program["metadata"], "uid": ids["configmap"]}}
        held = {"window_sha256": window.sha256, "policy_uid": str(uuid4()), "binding_uid": str(uuid4()),
                "policy_generation": 1, "binding_generation": 1}
        opened = dict(held, policy_generation=2, job_uid=ids["job"], expires_at=end)
        common = {"window_sha256": window.sha256, "run": window.run, "packet_file_sha256": save("packet", packet),
                  "deployment_sha256": "deploy", "expires_at": end, "cutoff": end, "staging_operator": "stage",
                  "storage_operator": "storage", "exclusive_storage_handoff": True}
        if owner_phase:
            common.update(purpose="public-flamingo-prepare-leaves", fresh_empty_leaves=True, leaf_operator="leaf",
                          owner_job_file_sha256=save("owner", owner_job))
        else:
            common.update(purpose="public-flamingo-stage", inventory_secret="inventory", inventory_uid=ids["inventory"],
                          inventory_secret_immutable=True)
        predecessor = save("enrolled", dict(common, action="enroll", closed_guard=held))
        active = dict(common, action="activate", enrollment_approval_sha256=predecessor)
        if owner_phase:
            active.update(owner_guard=opened, job_resource_version="20")
        else:
            active.update(stage_guard=opened, configmap_uid=ids["configmap"])
        approval_sha = save("approved", active)
        args = ["activate", "--approved-sha256", approval_sha, "--enrollment-approval", str(directory / "enrolled")]
        for arg, name_ in (("approval", "approved"), ("scope", "scope"), ("packet", "packet"), ("binding", "binding"),
                           ("promotion", "promotion"), ("window", "window"), ("window-scope", "scope")):
            args += ["--" + arg, str(directory / name_)]
        args += ["--window-sha256", window.sha256]
        if owner_phase:
            args += ["--owner-job", str(directory / "owner")]

        original = {**checked_job, "metadata": {**checked_job["metadata"], "uid": ids["job"],
                                                "resourceVersion": "20", "generation": 1}}
        build = Mock(return_value=packet)
        close = runpy.run_path(str(SKY / "public-flamingo-stage-closeout.py"))
        close["verify_job"] = Mock()
        close["exclude_other_writers"] = Mock()
        scripts = {"render-public-flamingo-stage.py": {"build_packet": build}, "submit-public-flamingo-stage.py": vars(submit),
                   "native-activation.py": {"held_policy_and_binding": lambda: (None, None),
                                            "check_active": Mock(), "POLICY": "policy", "GUARD": "guard"},
                   "public-flamingo-stage-closeout.py": close}
        def read(cmd, **kw):
            if "whoami" in cmd:
                return "storage" if kw.get("text") else b"storage"
            if "secret" in cmd:
                return ids["inventory"] + " true" + (" " + window.sha256 if "sha256" in cmd[-1] else "")
            obj = cm if "configmap" in cmd else original if "job" in cmd else {}
            return json.dumps(obj)
        writes = []
        def execute(cmd, **kw):
            if "patch" in cmd:
                writes.append(json.loads(cmd[cmd.index("--patch") + 1]))
                return SimpleNamespace(returncode=0, stdout=json.dumps(original))
            self.assertEqual(Path(cmd[1]).name, "training-storage-handoff.py")
            self.assertEqual(kw["env"]["NEBIUS_STORAGE_BINDING_SHA256"], "binding")

        config = directory / "profile"
        config.touch(mode=0o600)
        env = {"NEBIUS_TRAINING_CLUSTER_ID": "cluster", "NEBIUS_CA_BUNDLE_SHA256": "ca",
               "NEBIUS_STORAGE_KUBECONFIG": str(config), "NEBIUS_STORAGE_OPERATOR_IDENTITY": "storage"}
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env))
            stack.enter_context(patch.object(windows, "load_window", return_value=window))
            stack.enter_context(patch.object(windows, "verify_live_window"))
            guard = stack.enter_context(patch.object(guards, "verify_guard", return_value=opened))
            stack.enter_context(patch.object(private_api, "pinned_kubeconfig_ca"))
            stack.enter_context(patch.object(submit, "pinned_kubeconfig_ca"))
            stack.enter_context(patch.object(production_target, "kubectl_client_for_identity", return_value=["kubectl"]))
            stack.enter_context(patch.object(submit, "kubectl_client_for_identity", return_value=["kubectl"]))
            stack.enter_context(patch.object(submit, "check_placement"))
            stack.enter_context(patch.object(runpy, "run_path", side_effect=lambda path: scripts[Path(path).name]))
            stack.enter_context(patch.object(submit.subprocess, "check_output", side_effect=read))
            stack.enter_context(patch.object(submit.subprocess, "run", side_effect=execute))
            stack.enter_context(patch.object(sys, "argv", ["test"] + args))
            out = stack.enter_context(redirect_stdout(StringIO()))
            if owner_phase:
                owner["operate"](args)
            else:
                submit.main()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0]["value"], ids["job"])
        self.assertEqual(writes[0][1]["value"], "20")
        self.assertEqual(json.loads(out.getvalue())["window_sha256"], window.sha256)
        self.assertGreaterEqual(build.call_count, 2)
        self.assertTrue(all(call.kwargs["window"] is window for call in build.call_args_list))
        self.assertTrue(all(call.kwargs["window"] is window for call in guard.call_args_list))
