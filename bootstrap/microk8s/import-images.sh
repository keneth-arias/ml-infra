#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
docker build -t ml-infra/mlflow:3.18.0 platform/mlflow
docker build -t ml-infra/training:dev2 runtime
for image in ml-infra/mlflow:3.18.0 ml-infra/training:dev2; do
  docker save "$image" | microk8s ctr images import -
done
