#!/usr/bin/env bash
# Everything for a live demo in one go:
#   1. start the Kubernetes stack,
#   2. restore the saved hour of data (shifted to end now),
#   3. start the sensor simulator in the background,
#   4. open the dashboard.
# Stop it all with ./deploy/demo/stop-demo.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SENSORS="${SENSORS:-120}" RATE="${RATE:-2}" DURATION="${DURATION:-7200}"

"$ROOT/deploy/k8s/start.sh"
if [[ "${SKIP_RESTORE:-0}" != "1" ]]; then
  "$ROOT/deploy/demo/restore-demo-data.sh"
fi
[[ -x "$ROOT/.venv/bin/python" ]] || { python3 -m venv "$ROOT/.venv" && "$ROOT/.venv/bin/pip" install -q -r "$ROOT/bench/requirements.txt"; }
nohup "$ROOT/.venv/bin/python" -u "$ROOT/services/sensor-sim/sim.py" \
  --sensors "$SENSORS" --rate "$RATE" --duration "$DURATION" > "$ROOT/demo-data/simulator.log" 2>&1 &
echo $! > "$ROOT/demo-data/simulator.pid"
echo "simulator running (pid $(cat "$ROOT/demo-data/simulator.pid"), log demo-data/simulator.log)"
echo "Power BI: connect to $(ipconfig getifaddr en0 2>/dev/null || echo '<this Mac IP>'):5433"
[[ "${NO_OPEN:-0}" == "1" ]] || open http://localhost:8000 2>/dev/null || true
