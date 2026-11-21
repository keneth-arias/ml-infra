#!/usr/bin/env bash
# Verify the exact bundle on a CPU pod before reserving a GPU.
set -euo pipefail
target=dev-bm
kube=(kubectl)
base=$(dirname "$0")
if [[ ${1:-} == --nebius ]]; then
  shift
  target=prod-nebius
  export NEBIUS_SUBMIT_KUBECONFIG=${NEBIUS_SUBMIT_KUBECONFIG:-${NEBIUS_TRAINING_KUBECONFIG:?Set the scoped training submitter kubeconfig}}
  export KUBECONFIG="$NEBIUS_SUBMIT_KUBECONFIG"
  kube+=(--kubeconfig "$KUBECONFIG" --context "$target")
  python3 "$base/production_target.py" --check-training-submit
fi
mode=${1:-}
if [[ "$mode" != job && "$mode" != sky ]]; then
  echo 'Usage: launch-episode.sh [--nebius] {job|sky} {fit|infer} HEAD RUN_ID [profile options]' >&2
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
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
python3 "$base/episode-profile.py" preflight "$@" --target "$target" > "$tmp/pre.json"
receipt=$("${kube[@]}" create -f "$tmp/pre.json" -o name)
"${kube[@]}" -n training wait --for=condition=complete --timeout=10m "$receipt" || {
  "${kube[@]}" -n training logs "$receipt"
  exit 1
}
python3 "$base/episode-profile.py" "$mode" "$@" --target "$target" > "$tmp/task.yaml"
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
  python3 "$base/../../bootstrap/nebius/fit_attempt.py" reserve "$tmp/pre.json" "$tmp/task.yaml"
fi
if [[ "$mode" == job ]]; then
  "${kube[@]}" create -f "$tmp/task.yaml"
else
  sky launch -y -c "$cluster_name" "$tmp/task.yaml"
fi
