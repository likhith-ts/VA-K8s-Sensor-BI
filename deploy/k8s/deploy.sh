#!/usr/bin/env bash
# Build the service images and deploy the stack to the current kube context.
#   ./deploy/k8s/deploy.sh            build + deploy
#   ./deploy/k8s/deploy.sh --no-build deploy only
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
K8S="$ROOT/deploy/k8s"
TAG=1.0

if [[ "${1:-}" != "--no-build" ]]; then
  for svc in ingest process gateway sensor-sim; do
    docker build -q --build-arg SERVICE="$svc" -t "envbi/$svc:$TAG" "$ROOT"
  done
fi

# kind-based clusters (including Docker Desktop's kind mode) cannot see the
# local Docker image store.
if kubectl get nodes -o jsonpath='{.items[0].spec.providerID}' | grep -q '^kind://'; then
  "$K8S/load-images.sh" envbi/ingest:$TAG envbi/process:$TAG envbi/gateway:$TAG envbi/sensor-sim:$TAG
fi

if ! kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1; then
  echo "installing metrics-server (needed by the HPA)"
  "$K8S/install-metrics-server.sh"
fi

kubectl apply -f "$K8S/00-namespace.yaml"
kubectl -n envbi create configmap tsdb-init \
  --from-file=10-init.sql="$ROOT/db/init.sql" \
  --from-file=00-replication.sh="$K8S/tsdb-replication.sh" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -k "$K8S"

echo "waiting for the database primary and replica"
kubectl -n envbi rollout status statefulset/tsdb --timeout=300s
for d in ingest process gateway; do kubectl -n envbi rollout status deploy/$d --timeout=180s; done
kubectl -n envbi get pods,svc,hpa
echo
echo "dashboard:   http://localhost:8000"
echo "MQTT:        localhost:1883   (python services/sensor-sim/sim.py --sensors 100)"
echo "Power BI:    PostgreSQL localhost:5433, db envbi, user powerbi / powerbi"
