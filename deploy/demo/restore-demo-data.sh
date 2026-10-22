#!/usr/bin/env bash
# Load a saved hour of sensor data, shifted so it ends right now.
#
# The bi.* views show "the last 5 minutes / 24 hours", so data restored days
# later would be invisible. This script moves every timestamp forward by
# (now - newest reading), which makes the dashboard look as if the pipeline
# has been running for the past hour. It REPLACES what is in the readings
# and sensors tables.
#
#   ./deploy/demo/restore-demo-data.sh            # uses demo-data/
#   DB=envbi_test ./deploy/demo/restore-demo-data.sh
#
# Save a new snapshot with ./deploy/demo/save-demo-data.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DATA="${DATA_DIR:-$ROOT/demo-data}"
DB="${DB:-envbi}"
PSQL=(kubectl -n envbi exec -i tsdb-0 -c timescaledb -- psql -v ON_ERROR_STOP=1 -q -U envbi -d "$DB")

[[ -f "$DATA/readings.csv.gz" && -f "$DATA/sensors.csv.gz" ]] || { echo "no snapshot in $DATA"; exit 1; }

echo "staging the snapshot"
"${PSQL[@]}" <<'SQL'
SET client_min_messages = warning;
DROP TABLE IF EXISTS demo_readings, demo_sensors;
CREATE UNLOGGED TABLE demo_readings (LIKE readings);
CREATE UNLOGGED TABLE demo_sensors (LIKE sensors);
SQL
gunzip -c "$DATA/readings.csv.gz" | "${PSQL[@]}" -c "\copy demo_readings from pstdin with (format csv, header)"
gunzip -c "$DATA/sensors.csv.gz" | "${PSQL[@]}" -c "\copy demo_sensors from pstdin with (format csv, header)"

echo "shifting timestamps to end now and replacing current data"
"${PSQL[@]}" <<'SQL'
SELECT now() - max(ts) AS shift FROM demo_readings \gset
BEGIN;
TRUNCATE readings, sensors;
INSERT INTO readings (ts, sensor_id, sensor_group, seq, temperature_c, humidity, pressure_hpa, pm25, pm10, co2,
                      aqi, aqi_category, gap_filled, anomalies, ingest_ts, processed_ts)
SELECT ts + :'shift', sensor_id, sensor_group, seq, temperature_c, humidity, pressure_hpa, pm25, pm10, co2,
       aqi, aqi_category, gap_filled, anomalies, ingest_ts + :'shift', processed_ts + :'shift'
FROM demo_readings;
INSERT INTO sensors (sensor_id, sensor_group, first_seen, last_seen)
SELECT sensor_id, sensor_group, first_seen + :'shift', last_seen + :'shift' FROM demo_sensors;
COMMIT;
DROP TABLE demo_readings, demo_sensors;
SQL

echo "refreshing the 1m / 15m / 1h aggregates"
"${PSQL[@]}" <<'SQL'
CALL refresh_continuous_aggregate('readings_1m',  now() - INTERVAL '3 hours', now());
CALL refresh_continuous_aggregate('readings_15m', now() - INTERVAL '3 hours', now());
CALL refresh_continuous_aggregate('readings_1h',  now() - INTERVAL '3 hours', now());
SQL

"${PSQL[@]}" -Atc "SELECT 'restored ' || count(*) || ' readings, ' || min(ts)::time(0) || ' to ' || max(ts)::time(0) || ' UTC' FROM readings"
echo "start the simulator within 2 minutes so sensors stay 'online' and the cards keep updating"
