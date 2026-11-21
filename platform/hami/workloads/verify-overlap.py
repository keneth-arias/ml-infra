#!/usr/bin/env python3
"""Check private Pod, Node and CUDA stdout receipts from a verified managed API."""
import argparse
import json
from pathlib import Path
import re

IMAGE = "ghcr.io/keneth-arias/ml-infra-qwen@sha256:3378851e06efb96c8c975fc4cf2d2121e3a8efeb0ff03b392f5683e76258096b"
ALLOCATION = re.compile(r"(GPU-[0-9a-fA-F-]+),[^,;:]+,4096,25:;")
NANOSECONDS = 1_000_000_000
GIB = 1024**3


def check(condition, message):
    if not condition:
        raise ValueError(message)


def probe(pod, log, node_name):
    meta, spec, status = (pod[key] for key in ("metadata", "spec", "status"))
    check(meta["namespace"] == "hami-workloads"
          and meta["name"].startswith("hami-cuda-overlap-")
          and meta["labels"]["app.kubernetes.io/name"] == "hami-cuda-overlap"
          and spec["nodeName"] == node_name and spec["schedulerName"] == "hami-scheduler"
          and status["phase"] == "Succeeded", "Expected a completed managed pilot Pod")
    container, = spec["containers"]
    live, = status["containerStatuses"]
    check(container["image"] == IMAGE and live["imageID"].removeprefix("docker-pullable://") == IMAGE
          and live["state"]["terminated"]["exitCode"] == 0,
          "Pilot image or successful container exit could not be verified")
    for kind in ("requests", "limits"):
        values = container["resources"][kind]
        check({k: v for k, v in values.items() if "/" in k} ==
              {"hami.io/gpu": "1", "hami.io/gpumem": "4096", "hami.io/gpucores": "25"},
              "Pod did not request the exact fractional shape")
    allocation = ALLOCATION.fullmatch(meta["annotations"]["hami.io/vgpu-devices-allocated"])
    check(allocation, "No exact 4096 MiB / 25 core single-slot allocator receipt")
    events = [json.loads(line) for line in log.splitlines()
              if line.startswith('{"event": "hami_cuda_overlap",')]
    event, = events
    start, end = event["start_ns"], event["end_ns"]
    check(type(start) is int and type(end) is int and start > 0 and end - start >= 45 * NANOSECONDS
          and type(event["updates"]) is int and event["updates"] >= 2
          and 0 < event["total_memory_bytes"] <= 4 * GIB and event["torch_cuda"],
          "CUDA probe did not sustain verified operations within the reported memory bound")
    return meta["uid"], allocation[1], start, end


def verify(node, pods, logs, approved_provider_id):
    name = node["metadata"]["name"]
    labels = node["metadata"]["labels"]
    allocatable = node["status"]["allocatable"]
    check(node["spec"]["providerID"] == approved_provider_id and approved_provider_id.startswith("nebius://")
          and labels.get("gpu") == "on" and labels.get("airaware.training/gpu-mode") == "hami"
          and any(t == {"key": "airaware.training/gpu-mode", "value": "hami", "effect": "NoSchedule"}
                  for t in node["spec"].get("taints", []))
          and int(allocatable["hami.io/gpu"]) >= 2 and int(allocatable.get("nvidia.com/gpu", "0")) == 0,
          "Readback must show the approved isolated HAMi Node without native GPU capacity")
    a, b = [probe(pod, log, name) for pod, log in zip(pods, logs, strict=True)]
    check(a[0] != b[0] and a[1] == b[1], "Expected two distinct Pods allocated to the same physical GPU")
    overlap = min(a[3], b[3]) - max(a[2], b[2])
    check(overlap >= 15 * NANOSECONDS, "CUDA execution intervals did not overlap by at least 15 seconds")
    return {"node_uid": node["metadata"]["uid"], "pod_uids": [a[0], b[0]], "gpu_uuid": a[1],
            "cuda_activity_overlap_seconds": overlap / NANOSECONDS,
            "reported_cuda_memory_bound_bytes_per_pod": 4 * GIB,
            "allocated_gpu_core_percent_per_pod": 25}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--approved-provider-id", required=True)
    parser.add_argument("--pod", nargs=2, required=True, type=Path)
    parser.add_argument("--log", nargs=2, required=True, type=Path)
    args = parser.parse_args()
    try:
        paths = [args.node, *args.pod, *args.log]
        check(all(path.is_file() and path.stat().st_mode & 0o077 == 0 for path in paths),
              "Use private managed readback files")
        receipt = verify(
            json.loads(args.node.read_text()),
            [json.loads(path.read_text()) for path in args.pod],
            [path.read_text() for path in args.log],
            args.approved_provider_id,
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(1, f"CUDA overlap not verified: {exc}\n")
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
