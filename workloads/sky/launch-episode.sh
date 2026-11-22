#!/usr/bin/env bash
# Verify the exact bundle on a CPU pod before reserving a GPU.
set -euo pipefail
target=dev-bm
kube=(kubectl --context "${EPISODE_LOCAL_CONTEXT:-microk8s}")
base=$(dirname "$0")
if [[ ${1:-} == --nebius ]]; then
  shift
  umask 077
  target=prod-nebius
  export NEBIUS_SUBMIT_KUBECONFIG=${NEBIUS_SUBMIT_KUBECONFIG:-${NEBIUS_TRAINING_KUBECONFIG:?Set the scoped training submitter kubeconfig}}
  export KUBECONFIG="$NEBIUS_SUBMIT_KUBECONFIG"
  kube=(kubectl --kubeconfig "$KUBECONFIG" --context "$target")
  python3 "$base/production_target.py" --check-training-submit
fi
mode=${1:-}
if [[ "$mode" != job && "$mode" != sky && ! ( "$mode" == preflight && "$target" == prod-nebius ) ]]; then
  echo 'Usage: launch-episode.sh [--nebius] {job|sky|preflight} {fit|infer} HEAD RUN_ID [profile options]' >&2
  exit 2
fi
shift
for arg in "$@"; do
  case "$arg" in
    --target*) echo 'Select the cloud target with --nebius.' >&2; exit 2 ;;
  esac
done
if [[ "$target" == prod-nebius && "$mode" == sky ]]; then
  python3 "$base/production_target.py" training
fi
if [[ "$target" == dev-bm ]]; then
  resources=$("${kube[@]}" --request-timeout=10s get node bm -o \
    'jsonpath={.status.conditions[?(@.type=="Ready")].status}{" "}{.status.allocatable.nvidia\.com/gpu}')
  read -r ready gpus <<< "$resources"
  if [[ "$ready" != True || ! "$gpus" =~ ^[1-9][0-9]*$ ]]; then
    echo 'The local episode target needs a Ready bm node with an allocatable GPU.' >&2
    exit 1
  fi
fi
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
render_task() {
  local format=$mode
  if [[ $format == preflight ]]; then format=job; fi
  python3 "$base/episode-profile.py" "$format" "$@" --target "$target" > "$tmp/task.yaml"
}
check_release() {
  python3 "$base/production_release.py" check-episode "$tmp/pre.json" "$tmp/task.yaml"
}
python3 "$base/episode-profile.py" preflight "$@" --target "$target" > "$tmp/pre.json"
if [[ "$target" == prod-nebius ]]; then
  render_task "$@"
  approved_receipt=$(check_release)
  if [[ ! "$approved_receipt" =~ ^[0-9a-f]{64}$ ]]; then
    echo 'Production release verification returned an invalid receipt digest.' >&2
    exit 1
  fi
  python3 "$base/training-storage-handoff.py"
fi
receipt=$("${kube[@]}" create -f "$tmp/pre.json" -o name)
"${kube[@]}" -n training wait --for=condition=complete --timeout=10m "$receipt" || {
  "${kube[@]}" -n training logs "$receipt"
  exit 1
}
if [[ "$mode" == preflight ]]; then
  printf '%s\n' "$receipt"
  exit 0
fi
if [[ "$target" == dev-bm ]]; then render_task "$@"; fi
if [[ "$mode" == sky ]]; then
  cluster_name=$3
  if [[ $target == prod-nebius ]]; then cluster_name="nebius-$3"; fi
  owned=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["config"]["kubernetes"]["pod_config"]["metadata"]["labels"].get("airaware.training/sky-cluster",""))' "$tmp/task.yaml")
  if [[ -n $owned ]]; then
    cluster_name=$owned
    sky status -o json "$owned" | python3 -c 'import json,sys; sys.exit(bool(json.load(sys.stdin)))' || {
      echo "A cluster already exists for this fit attempt, or its status is unavailable." >&2
      exit 1
    }
  fi
fi
if [[ "$target" == prod-nebius ]]; then
  if [[ $mode == sky ]]; then python3 "$base/production_target.py" training; fi
  python3 "$base/training-storage-handoff.py"
  current_receipt=$(check_release)
  if [[ "$current_receipt" != "$approved_receipt" ]]; then
    echo 'The approved production release changed during preflight. Run preflight again.' >&2
    exit 1
  fi
  python3 "$base/../../bootstrap/nebius/fit_attempt.py" reserve "$tmp/pre.json" "$tmp/task.yaml"
fi
if [[ "$mode" == job ]]; then
  "${kube[@]}" create -f "$tmp/task.yaml"
else
  sky launch -y -c "$cluster_name" "$tmp/task.yaml"
fi
