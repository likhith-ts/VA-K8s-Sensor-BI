#!/usr/bin/env bash
# Save the current readings and sensors to demo-data/ for restore-demo-data.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="${DATA_DIR:-$ROOT/demo-data}"
mkdir -p "$DATA"
PSQL=(kubectl -n envbi exec -i tsdb-0 -c timescaledb -- psql -v ON_ERROR_STOP=1 -q -U envbi -d "${DB:-envbi}")
"${PSQL[@]}" -c "\copy (select * from readings order by ts) to stdout with (format csv, header)" | gzip -6 > "$DATA/readings.csv.gz"
"${PSQL[@]}" -c "\copy sensors to stdout with (format csv, header)" | gzip > "$DATA/sensors.csv.gz"
echo "saved $(($(gunzip -c "$DATA/readings.csv.gz" | wc -l) - 1)) readings to $DATA"
