#!/usr/bin/env bash
set -euo pipefail
if (( $# != 3 )); then
  echo 'Usage: build-episodes.sh OpenTSLM_CHECKOUT AIR_AWARE_CHECKOUT IMAGE_TAG' >&2
  exit 2
fi
infra=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
forecast_sha=$(git -C "$1" rev-parse HEAD)
reader_sha=$(git -C "$2" rev-parse HEAD)
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
mkdir "$stage/forecast" "$stage/reader"
git -C "$1" archive "$forecast_sha" src/opentslm scripts/train_qwen_forecast.py | tar -x -C "$stage/forecast"
git -C "$2" archive "$reader_sha:tools/timenet-smoke" | tar -x -C "$stage/reader"
infra_sha=$(git -C "$infra" rev-parse HEAD)
git -C "$infra" archive "$infra_sha:runtime" episode_job.py Dockerfile.episodes | tar -x -C "$stage"
docker build --build-arg "INFRA_SOURCE_REVISION=$infra_sha" -f "$stage/Dockerfile.episodes" -t "$3" \
  --build-arg "FORECAST_SOURCE_REVISION=$forecast_sha" --build-arg "AIRAWARE_SOURCE_REVISION=$reader_sha" "$stage"
docker image inspect "$3" --format '{{json .RepoDigests}}'
