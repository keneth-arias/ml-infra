"""Exercise the offline projection, exact input scope, and fixed paired workload."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest

SMOKE = Path(__file__).resolve().parents[1] / "workloads/smoke"
R = runpy.run_path(str(SMOKE / "render-flamingo-baseline.py"))
sys.path.insert(0, str(SMOKE / "arfbench"))
from prepare import canonical, project
from worker import verify_tree


class ArfbenchTest(unittest.TestCase):
    def test_exact_render(self):
        pins, inventory = R["arfbench_inputs"]()
        job = R["render_arfbench"](pins["image"], "pair-one", "gpu-1", 1800, 40000000000)
        pod = job["spec"]["template"]["spec"]
        self.assertTrue(job["spec"]["suspend"])
        self.assertEqual(job["metadata"]["name"], "arfbench-pair-one")
        container, = pod["containers"]
        self.assertEqual(container["image"], pins["image"])
        self.assertEqual(container["command"][4], (SMOKE/"arfbench/worker.py").read_text())
        self.assertEqual(json.loads(container["command"][5]), pins)
        self.assertEqual(container["resources"]["limits"]["nvidia.com/gpu"], "1")
        self.assertFalse(pod["automountServiceAccountToken"])
        mounts = container["volumeMounts"]
        self.assertEqual([m["mountPath"] for m in mounts], ["/base", "/bench", "/output", "/tmp"])
        self.assertEqual([m["readOnly"] for m in mounts[:2]], [True, True])
        self.assertEqual(mounts[1]["subPath"], "arfbench-inputs/" + inventory)
        self.assertEqual(mounts[2]["subPath"], "arfbench-runs/pair-one")
        self.assertEqual(set(pins["splits"]), {"test", "swap"})
        with self.assertRaisesRegex(ValueError, "immutable"):
            R["render_arfbench"](pins["image"][:-1] + "0", "pair-one", "gpu-1", 1800, 1)

    def test_verify_set_bytes_and_links(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            file = root / "sample"
            file.write_bytes(b"ok")
            pins = {"sample":hashlib.sha256(b"ok").hexdigest()}
            verify_tree(root,pins)
            (root/"extra").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError,"file set"):
                verify_tree(root,pins)
            (root/"extra").unlink()
            (root/"link").symlink_to(file)
            with self.assertRaisesRegex(ValueError,"type"):
                verify_tree(root,pins)
            (root/"link").unlink()
            file.write_bytes(b"no")
            with self.assertRaisesRegex(ValueError,"digest"):
                verify_tree(root,pins)

    def test_projection_preserves_only_test_examples_and_values(self):
        rows = [dict(id=i, split=s, exclusion=e, series=[k], options=["cpu","mem"])
                for i,s,e,k in [(1,"train",None,"t"), (2,"test",None,"a"), (3,"test","absent","x")]]
        parent = dict(schema=1, dataset="datadog/arfbench", version="1.0.0", max_values=4096, max_series=32,
                      rows=rows, traces={k:{"values":[0,None,3]} for k in ("t","a","x")})
        parent["sha256"] = hashlib.sha256(canonical(parent)).hexdigest()
        full = canonical(parent)
        view = json.loads(project(full,hashlib.sha256(full).hexdigest()))
        self.assertEqual(view["parent_sha256"],parent["sha256"])
        self.assertEqual(view["traces"],{"a":parent["traces"]["a"]})
        self.assertEqual(view["rows"],[copy.deepcopy(rows[1]), dict(rows[2],series=[])])
        with self.assertRaisesRegex(ValueError,"different frozen"):
            project(full,"0"*64)
