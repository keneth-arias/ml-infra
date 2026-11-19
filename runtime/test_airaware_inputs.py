"""Verify the input version and GPU requests used by the training launcher."""

import hashlib
import json
import os
import subprocess
import tempfile
from textwrap import dedent
import unittest
from pathlib import Path

from airaware import window_inputs


class WorkloadTest(unittest.TestCase):
    def test_versioned_inputs_match_the_frozen_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(window_inputs(root, "pilot")[0], root)
            (root / "timef").mkdir()
            (root / "timef/manifest.json").write_bytes(b"dataset")
            bundle = root / "windows/v1"
            bundle.mkdir(parents=True)
            splits = {}
            for split in ("train", "validation"):
                data = split.encode()
                (bundle / f"{split}.npz").write_bytes(data)
                splits[split] = {"npz_sha256": hashlib.sha256(data).hexdigest()}
            manifest = {
                "version": "v1",
                "inputs": splits,
                "timef_manifest_sha256": hashlib.sha256(b"dataset").hexdigest(),
            }
            (bundle / "manifest.json").write_text(json.dumps(manifest))
            self.assertEqual(window_inputs(root, "v1")[0], bundle)
            (bundle / "train.npz").write_bytes(b"changed")
            with self.assertRaises(ValueError):
                window_inputs(root, "v1")
            with self.assertRaises(ValueError):
                window_inputs(root, "../v1")

    def test_launcher_chooses_one_or_two_devices(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "sky"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            fake.chmod(0o755)
            launcher = Path(__file__).resolve().parents[1] / "workloads/sky/launch-airaware.sh"
            env = {**os.environ, "PATH": f"{temp}:{os.environ['PATH']}"}
            for mode, count in (("single", 1), ("ddp", 2), ("fsdp", 2), ("pair", 2)):
                args = subprocess.check_output(
                    [launcher, mode, "cluster", "run"], text=True, env=env
                ).splitlines()
                self.assertEqual(args[args.index("--gpus") + 1], f"rtx5070ti:{count}")
                self.assertIn(f"TRAIN_MODE={mode}", args)
                if mode == "pair":
                    self.assertEqual(args[args.index("--memory") + 1], "40")
                    self.assertEqual(args[args.index("--cpus") + 1], "8")

    def test_pair_assigns_visible_cards_and_propagates_a_failed_head(self):
        task = Path(__file__).resolve().parents[1] / "workloads/sky/airaware.yaml"
        command = dedent(task.read_text().split("run: |\n")[-1])
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "train"
            fake.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$CUDA_VISIBLE_DEVICES" "$@" > "$LOGS/$1"\ncase "$1" in *direct) exit 7 ;; esac\n'
            )
            fake.chmod(0o755)
            command = command.replace("/opt/conda/bin/python /opt/ml-infra/airaware.py", str(fake))
            env = dict(
                os.environ,
                TRAIN_MODE="pair",
                RUN_ID="cmp",
                CUDA_VISIBLE_DEVICES="GPU-a,GPU-b",
                HEAD="direct",
                LORA="0",
                RESUME="0",
                PRECISION="fp32",
                STOP_STEP="2",
                SCHEDULE_STEPS="2",
                SAVE_SECONDS="900",
                BATCH="4",
                INPUT_VERSION="v1",
                VALIDATE_AT="final",
                LOGS=temp,
            )
            result = subprocess.run(["bash", "-c", command], env=env)
            self.assertEqual(result.returncode, 7)
            for head, device in (("direct", "GPU-a"), ("persistence", "GPU-b")):
                args = (Path(temp) / f"cmp-{head}").read_text().splitlines()
                self.assertEqual(args[0], device)
                self.assertEqual(args[args.index("--mode") + 1], "single")
                self.assertEqual(args[args.index("--head") + 1], head)
