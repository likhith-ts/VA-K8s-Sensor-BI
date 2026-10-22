#!/usr/bin/env bash
# Stop every workload but keep the cluster objects and data volumes.
# Start again with ./deploy/k8s/start.sh.
set -euo pipefail
kubectl -n envbi scale deploy --all --replicas=0
kubectl -n envbi scale statefulset --all --replicas=0
echo "envbi stopped (data volumes kept)"
