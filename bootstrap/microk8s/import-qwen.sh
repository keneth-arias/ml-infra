#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "Usage: $0 FORECAST_CHECKOUT AIR_AWARE_CHECKOUT" >&2
  exit 2
fi
cd "$(git rev-parse --show-toplevel)"
forecast=$(realpath "$1")
airaware=$(realpath "$2")
revision=$(git -C "$forecast" rev-parse HEAD)
python=$(uv python find --managed-python 3.13.13)
python_root=$(dirname "$(dirname "$(readlink -f "$python")")")
context=$(mktemp -d)
trap 'rm -rf -- "$context"' EXIT
mkdir "$context/reader" "$context/forecast"
git -C "$forecast" archive "$revision" src scripts/train_qwen_forecast.py |
  tar -x -C "$context/forecast"
for file in pyproject.toml uv.lock qwen_window.py senamhi_timef.py export_senamhi.py; do
  cp "$airaware/tools/timenet-smoke/$file" "$context/reader/"
done
image="ml-infra/training:qwen-${revision:0:12}-r2"
docker build --build-arg FORECAST_SOURCE_REVISION="$revision" \
  --build-context "forecast=$context/forecast" --build-context "reader=$context/reader" \
  --build-context "reader-python=$python_root" \
  -t "$image" -f runtime/Dockerfile.qwen .
docker save "$image" | sudo -n microk8s ctr image import -
docker image inspect "$image" --format '{{.Id}}'
