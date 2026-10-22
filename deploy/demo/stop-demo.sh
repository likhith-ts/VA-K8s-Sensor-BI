#!/usr/bin/env bash
# Stop the simulator and the stack started by start-demo.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
if [[ -f "$ROOT/demo-data/simulator.pid" ]]; then
  kill "$(cat "$ROOT/demo-data/simulator.pid")" 2>/dev/null || true
  rm -f "$ROOT/demo-data/simulator.pid"
fi
pkill -f "services/sensor-sim/sim.py" 2>/dev/null || true
"$ROOT/deploy/k8s/stop.sh"
