#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 || $# > 4 )) || [[ "${4:-}" == "$3" ]]; then
  echo 'Usage: build-episodes.sh OpenTSLM_CHECKOUT AIR_AWARE_CHECKOUT TRAINER_TAG [DISTINCT_PREFLIGHT_TAG]' >&2
  exit 2
fi
case "${FORECAST_BACKBONE:-qwen}" in
  qwen|flamingo) backbone="${FORECAST_BACKBONE:-qwen}" ;;
  *) echo 'FORECAST_BACKBONE must be qwen or flamingo.' >&2; exit 2 ;;
esac
infra=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)
forecast_sha=$(git -C "$1" rev-parse HEAD)
reader_sha=$(git -C "$2" rev-parse HEAD)
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
mkdir "$stage/forecast" "$stage/reader"
git -C "$1" archive "$forecast_sha" src/opentslm scripts/train_qwen_forecast.py | tar --same-permissions -x -C "$stage/forecast"
if [[ "$backbone" == flamingo ]]; then
  reference_checkout=${FLAMINGO_REFERENCE_CHECKOUT:?Set FLAMINGO_REFERENCE_CHECKOUT to the AWS samples git checkout}
  reference_commit=7967032bfb7dc7925fbe68cd3c3c0daffd13e669
  reference_path=examples/opentslm-finetune-arfbench/scripts/utils.py
  mkdir "$stage/forecast/reference"
  git -C "$reference_checkout" show "$reference_commit:$reference_path" > "$stage/forecast/reference/utils.py"
  git -C "$reference_checkout" show "$reference_commit:LICENSE" > "$stage/forecast/reference/LICENSE"
  git -C "$1" show "$forecast_sha:scripts/check_per_series_reference.py" > "$stage/forecast/reference/check_per_series_reference.py"
  (cd "$stage/forecast/reference" && echo 'c19db31d2ce6e84a4ce02c9a0fdfe0869c53775f77b04dd4be5c893afd8335e2  utils.py' | sha256sum --check --strict)
fi
git -C "$2" archive "$reader_sha:tools/timenet-smoke" | tar --same-permissions -x -C "$stage/reader"
infra_sha=$(git -C "$infra" rev-parse HEAD)
git -C "$infra" archive "$infra_sha:runtime" episode_job.py episode_stop.py stage_episode_inputs.py versioned_episode_inputs.py Dockerfile.episodes Dockerfile.preflight | tar --same-permissions -x -C "$stage"
check_startup() {
  if [[ "$backbone" == flamingo ]]; then
    git -C "$infra" show "$infra_sha:ci/check-flamingo-image-startup.py" |
      docker run -i --rm --pull=never --network none --user 1000:1000 --cap-drop ALL \
        --read-only --tmpfs /tmp -e HOME=/tmp -e CUDA_VISIBLE_DEVICES= -e HF_HUB_OFFLINE=1 \
        --entrypoint "$3" "$2" -I -B - "$1"
  fi
}
docker build --build-arg "FORECAST_BACKBONE=$backbone" --build-arg "INFRA_SOURCE_REVISION=$infra_sha" -f "$stage/Dockerfile.episodes" -t "$3" \
  --build-arg "FORECAST_SOURCE_REVISION=$forecast_sha" --build-arg "AIRAWARE_SOURCE_REVISION=$reader_sha" "$stage"
check_startup trainer "$3" /opt/conda/bin/python
docker image inspect "$3" --format '{{json .RepoDigests}}'
if (( $# == 4 )); then
  docker build -f "$stage/Dockerfile.preflight" -t "$4" \
    --build-arg "INFRA_SOURCE_REVISION=$infra_sha" --build-arg "FORECAST_SOURCE_REVISION=$forecast_sha" "$stage"
  check_startup preflight "$4" /usr/local/bin/python
  docker image inspect "$4" --format '{{json .RepoDigests}}'
fi
