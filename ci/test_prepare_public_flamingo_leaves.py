from copy import deepcopy
import fcntl
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4
import runpy
import tempfile
import unittest
from unittest.mock import patch


owner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "workloads/sky/prepare-public-flamingo-leaves.py"))
prepare = owner["prepare"]


def as_approved_owner():
    real_fstat = os.fstat
    def owner_stat(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_uid=1000, st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino)
    return patch("os.fstat", side_effect=owner_stat)


class TwoNewLeaves(unittest.TestCase):
    def test_owner_job_has_only_two_leaves_and_chown(self):
        mounts = [{"name": "claim", "mountPath": "/base", "subPath": "weights/public"},
                  {"name": "claim", "mountPath": "/output", "subPath": "output/run"}]
        pod = {"containers": [{"image": "signed-ref", "volumeMounts": mounts + [{"name": "inventory"}]}],
               "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
               "priority": 0, "preemptionPolicy": "PreemptLowerPriority", "tolerations": [],
               "nodeSelector": {"pool": "cpu"}, "affinity": {}, "imagePullSecrets": []}
        job = owner["owner_job"]({"metadata": {"name": "stage-run", "labels": {"airaware.training/run": "run"}}, "spec": {"template": {"spec": pod}}}, "source")
        spec = job["spec"]["template"]["spec"]
        container, = spec["containers"]
        self.assertEqual(container["volumeMounts"], mounts)
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"], "add": ["CHOWN"]})
        self.assertEqual(len(spec["volumes"]), 1)
        self.assertFalse(spec["automountServiceAccountToken"])
        self.assertNotIn("fsGroup", spec["securityContext"])

    def test_rejects_entries_before_any_ownership_change(self):
        with tempfile.TemporaryDirectory() as directory:
            base, output = [Path(directory) / name for name in ("base", "output")]
            base.mkdir()
            output.mkdir()
            (output / ".previous").touch()
            with as_approved_owner(), patch("os.fchown") as chown, self.assertRaises(ValueError):
                prepare([base, output])
            chown.assert_not_called()

    def test_refuses_duplicate_mount_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "base"
            base.mkdir()
            link = base.with_name("alias")
            link.symlink_to(base)
            with self.assertRaises((ValueError, OSError)):
                prepare([base, link])
            with as_approved_owner(), self.assertRaises(ValueError):
                prepare([base, base])


class OwnerActivation(unittest.TestCase):
    def setUp(self):
        sky = Path(__file__).resolve().parents[1] / "workloads/sky"
        sys.path.insert(0, str(sky))
        self.closeout = runpy.run_path(str(sky / "public-flamingo-stage-closeout.py"))
        stage = runpy.run_path(str(sky / "render-public-flamingo-stage.py"))["stage_job"](
            "test", "stage-test-inventory", "stage-test-program", "signed-image", "a", "b")
        self.stage = stage
        self.job = owner["owner_job"](stage, owner["worker_source"]())
        self.job_uid = str(uuid4())
        self.live = deepcopy(self.job)
        self.live["metadata"].update(uid=self.job_uid, resourceVersion="19", generation=1)
        template = self.live["spec"]["template"]
        template["metadata"]["labels"].update({
            "job-name": "stage-test-owner", "batch.kubernetes.io/job-name": "stage-test-owner",
            "controller-uid": self.job_uid, "batch.kubernetes.io/controller-uid": self.job_uid})
        template["spec"]["serviceAccount"] = template["spec"]["serviceAccountName"]
        self.live["spec"].update(manualSelector=False, completionMode="NonIndexed",
                                 selector={"matchLabels": {"batch.kubernetes.io/controller-uid": self.job_uid}})
        self.closed = {"policy_uid": str(uuid4()), "policy_generation": 7, "binding_uid": str(uuid4()), "binding_generation": 1}

    def verify(self):
        return owner["verify_owner_job"](self.closeout, self.job, self.live, self.job_uid,
                                         suspended=True, expected_rv="19")

    def test_successor_owner_enrollment_preserves_exact_signed_window_annotation(self):
        annotation = {"airaware.training/stage-window-sha256": "a" * 64}
        self.stage["metadata"]["annotations"] = annotation
        self.job = owner["owner_job"](self.stage, owner["worker_source"]())
        self.assertEqual(self.job["metadata"]["annotations"], annotation)
        with self.assertRaisesRegex(ValueError, "annotations"):
            self.verify()
        self.live["metadata"]["annotations"] = annotation
        self.assertEqual(self.verify()["uid"], self.job_uid)

    def test_only_pristine_owner_job_at_enrolled_uid_rv_can_release(self):
        self.assertEqual(self.verify()["uid"], self.job_uid)
        patch_ops = owner["owner_patch"](self.job_uid, "19")
        self.assertEqual([row["op"] for row in patch_ops], ["test", "test", "test", "replace"])
        self.assertEqual([row["value"] for row in patch_ops], [self.job_uid, "19", True, False])
        for field, changed in (("uid", str(uuid4())), ("resourceVersion", "20"), ("generation", 2)):
            previous = self.live["metadata"][field]
            self.live["metadata"][field] = changed
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify()
            self.live["metadata"][field] = previous
        self.live["status"] = {"failed": 1}
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.verify()

    def test_extra_mount_or_broad_capability_rejected(self):
        container = self.live["spec"]["template"]["spec"]["containers"][0]
        self.assertNotIn("kubectl", container["command"][4])
        self.assertNotIn("verify_guard", container["command"][4])
        with self.assertRaises(ValueError):
            container["volumeMounts"][0].pop("subPath")
            self.verify()

    def test_predecessor_prevents_role_guard_expiry_or_packet_substitution(self):
        common = {k: k for k in ("purpose", "run", "deployment_sha256", "packet_file_sha256", "owner_job_file_sha256",
                                 "leaf_operator", "storage_operator", "staging_operator", "exclusive_storage_handoff",
                                 "fresh_empty_leaves", "cutoff", "expires_at")}
        enrolled = {**common, "action": "enroll", "closed_guard": self.closed}
        active = {**common, "action": "activate", "owner_guard": {
            **self.closed, "job_uid": self.job_uid, "policy_generation": 8, "expires_at": common["expires_at"]}}
        owner["owner_transition"](enrolled, active)
        for field in common:
            with self.subTest(field=field), self.assertRaises(ValueError):
                owner["owner_transition"](enrolled, {**active, field: "changed"})
        for field, value in (("binding_uid", str(uuid4())), ("policy_generation", 9), ("binding_generation", 2)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                owner["owner_transition"](enrolled, {**active, "owner_guard": {**active["owner_guard"], field: value}})

    def test_locked_leaf_prevents_any_chown(self):
        with tempfile.TemporaryDirectory() as directory:
            base, output = [Path(directory) / name for name in ("base", "output")]
            base.mkdir()
            output.mkdir()
            fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with as_approved_owner(), patch("os.fchown") as chown, self.assertRaises(BlockingIOError):
                    prepare([base, output])
                chown.assert_not_called()
            finally:
                os.close(fd)
