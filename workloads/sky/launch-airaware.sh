#!/usr/bin/env bash
set -euo pipefail

if (( $# < 3 )); then
  echo "Usage: $0 {single|ddp|fsdp|pair|joint} CLUSTER RUN_ID [sky launch options]" >&2
  exit 2
fi
mode=$1
cluster=$2
run_id=$3
shift 3
resources=()
case "$mode" in
  single) cards=1 ;;
  ddp|fsdp) cards=2 ;;
  pair|joint) cards=2; resources=(--cpus 8 --memory 36) ;;
  *) echo "Unsupported training mode: $mode" >&2; exit 2 ;;
esac
task="$(dirname "$0")/airaware.yaml"
if [[ "${AIRAWARE_MODEL_STATE:-0}" == 1 || "$mode" == joint ]]; then
  rendered=$(mktemp --suffix=.yaml)
  trap 'rm -f -- "$rendered"' EXIT
  uv run --script "$(dirname "$0")/model-state.py" "$task" "$mode" > "$rendered"
  task=$rendered
fi
sky launch "$task" "${resources[@]}" "$@" --cluster "$cluster" --gpus "rtx5070ti:$cards" \
  --env "TRAIN_MODE=$mode" --env "RUN_ID=$run_id"
