from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch


owner = runpy.run_path(str(Path(__file__).resolve().parents[1] / "workloads/sky/prepare-public-flamingo-leaves.py"))
prepare = owner["prepare"]


class TwoNewLeaves(unittest.TestCase):
    def test_owner_job_has_only_two_leaves_and_chown(self):
        mounts = [{"name": "claim", "mountPath": "/base", "subPath": "weights/public"},
                  {"name": "claim", "mountPath": "/output", "subPath": "output/run"}]
        pod = {"containers": [{"image": "signed-ref", "volumeMounts": mounts + [{"name": "inventory"}]}],
               "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
               "nodeSelector": {"pool": "cpu"}, "affinity": {}, "imagePullSecrets": []}
        job = owner["owner_job"]({"metadata": {"name": "stage-run"}, "spec": {"template": {"spec": pod}}}, "source")
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
            with patch("os.fchown") as chown, self.assertRaises(ValueError):
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
            with self.assertRaises(ValueError):
                prepare([base, base])
