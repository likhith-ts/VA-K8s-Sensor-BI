#!/usr/bin/env bash
# Bring a stopped stack back to its normal size (after ./deploy/k8s/stop.sh
# or a Docker Desktop restart). First-time setup is ./deploy/k8s/deploy.sh.
set -euo pipefail
K8S="$(cd "$(dirname "$0")" && pwd)"
kubectl get namespace envbi >/dev/null 2>&1 || { echo "stack not deployed yet: run ./deploy/k8s/deploy.sh"; exit 1; }
# Re-applying the manifests restores the replica counts they declare.
kubectl apply -k "$K8S" >/dev/null
kubectl -n envbi rollout status statefulset/tsdb --timeout=300s
kubectl -n envbi rollout status statefulset/redis --timeout=120s
for d in mosquitto ingest process gateway; do kubectl -n envbi rollout status deploy/$d --timeout=180s; done
echo "stack is up: dashboard http://localhost:8000 · Power BI on port 5433"
