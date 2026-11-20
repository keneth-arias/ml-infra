#!/usr/bin/env bash
# Verify the exact bundle on a CPU pod before reserving a GPU.
set -euo pipefail
mode=${1:-}
if [[ "$mode" != job && "$mode" != sky ]]; then
  echo 'Usage: launch-episode.sh {job|sky} {fit|infer} HEAD RUN_ID [profile options]' >&2
  exit 2
fi
shift
base=$(dirname "$0")
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
python3 "$base/episode-profile.py" preflight "$@" > "$tmp/pre.json"
receipt=$(kubectl create -f "$tmp/pre.json" -o name)
kubectl -n training wait --for=condition=complete --timeout=10m "$receipt" || {
  kubectl -n training logs "$receipt"
  exit 1
}
python3 "$base/episode-profile.py" "$mode" "$@" > "$tmp/task.yaml"
if [[ "$mode" == job ]]; then
  kubectl create -f "$tmp/task.yaml"
else
  sky launch -y -c "$3" "$tmp/task.yaml"
fi
