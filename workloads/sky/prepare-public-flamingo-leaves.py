"""Optional storage-owner operation inside two newly bound, empty subPath mounts."""

import json
import os
import subprocess
import sys
from pathlib import Path
import stat
from contextlib import ExitStack


def prepare(directories):
    with ExitStack() as opened:
        leaves = []
        for path in directories:
            fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY)
            opened.callback(os.close, fd)
            info = os.fstat(fd)
            if (info.st_uid not in (0, 1000) or os.listdir(fd) or info.st_mode & 0o700 != 0o700
                    or info.st_mode & (stat.S_ISUID | stat.S_ISVTX)):
                raise ValueError("Expected fresh empty leaf directories with owner rwx")
            leaves.append((fd, info))
        if len({(info.st_dev, info.st_ino) for _, info in leaves}) != 2:
            raise ValueError("Expected two distinct leaves")
        for fd, info in leaves:
            if info.st_uid == 0:
                os.fchown(fd, 1000, -1)
            if os.fstat(fd).st_uid != 1000 or os.listdir(fd):
                raise ValueError("Owner operation requires attended reconciliation")
            os.fsync(fd)


def owner_job(stage, source):
    pod = stage["spec"]["template"]["spec"]
    container = pod["containers"][0]
    owner = {
        "name": "prepare-leaves", "image": container["image"],
        "command": ["/usr/local/bin/python", "-I", "-B", "-c", source, "--prepare-two-leaves"],
        "resources": {"requests": {"cpu": "100m", "memory": "32Mi"}, "limits": {"cpu": "1", "memory": "64Mi"}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"], "add": ["CHOWN"]}},
        "volumeMounts": [mount for mount in container["volumeMounts"] if mount["name"] == "claim"],
    }
    pod = {key: pod[key] for key in ("restartPolicy", "automountServiceAccountToken", "enableServiceLinks",
                                     "nodeSelector", "affinity", "imagePullSecrets")}
    pod.update(serviceAccountName="default", containers=[owner],
               securityContext={"runAsUser": 0, "runAsGroup": 0, "seccompProfile": {"type": "RuntimeDefault"}},
               volumes=[{"name": "claim", "persistentVolumeClaim": {"claimName": "airaware-checkpoints"}}])
    return {"apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": stage["metadata"]["name"] + "-owner", "namespace": "training"},
            "spec": {"parallelism": 1, "completions": 1, "backoffLimit": 0, "podReplacementPolicy": "Failed",
                     "activeDeadlineSeconds": 60, "template": {"spec": pod}}}


def main():
    if sys.argv[1:2] == ["render"]:
        # Render from the same verified allocation and source as the stager.
        script = Path(__file__).resolve()
        reviewed = subprocess.check_output([sys.executable, str(script.with_name("render-public-flamingo-stage.py")),
                                            *sys.argv[2:]])
        print(json.dumps(owner_job(json.loads(reviewed)["job"], script.read_text()), indent=2, sort_keys=True))
        return
    if sys.argv[1:] != ["--prepare-two-leaves"]:
        raise SystemExit("Use render with the public staging renderer arguments")
    paths = [Path("/base"), Path("/output")]
    mounts = {line.split()[4] for line in Path("/proc/self/mountinfo").read_text().splitlines()}
    if os.geteuid() != 0 or not {str(path) for path in paths} <= mounts:
        raise SystemExit("Expected the approved owner process and two exact bind mounts")
    prepare(paths)


if __name__ == "__main__":
    main()
