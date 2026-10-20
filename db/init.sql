-- Time-series store for the environmental BI pipeline (TimescaleDB).
--
-- readings            raw, cleaned readings (hypertable, compressed after 1 day)
-- readings_1m/15m/1h  rolling-window continuous aggregates
-- bi.*                curated views that Power BI reads via DirectQuery

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS sensors (
    sensor_id     text PRIMARY KEY,
    sensor_group  text NOT NULL,
    first_seen    timestamptz NOT NULL,
    last_seen     timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    ts             timestamptz NOT NULL,
    sensor_id      text        NOT NULL,
    sensor_group   text        NOT NULL,
    seq            bigint,
    temperature_c  double precision,
    humidity       double precision,
    pressure_hpa   double precision,
    pm25           double precision,
    pm10           double precision,
    co2            double precision,
    aqi            integer,
    aqi_category   text,
    gap_filled     text[]      NOT NULL DEFAULT '{}',
    anomalies      text[]      NOT NULL DEFAULT '{}',
    ingest_ts      timestamptz NOT NULL,
    processed_ts   timestamptz NOT NULL,
    PRIMARY KEY (sensor_id, ts)
);

SELECT create_hypertable('readings', 'ts', chunk_time_interval => INTERVAL '6 hours', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS readings_group_ts ON readings (sensor_group, ts DESC);
CREATE INDEX IF NOT EXISTS readings_processed_ts ON readings (processed_ts DESC);

ALTER TABLE readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'sensor_id',
    timescaledb.compress_orderby = 'ts DESC'
);
SELECT add_compression_policy('readings', INTERVAL '1 day', if_not_exists => TRUE);
SELECT add_retention_policy('readings', INTERVAL '30 days', if_not_exists => TRUE);

-- Alert thresholds, editable at runtime and joinable from Power BI.
CREATE TABLE IF NOT EXISTS thresholds (
    metric    text PRIMARY KEY,
    label     text NOT NULL,
    unit      text NOT NULL,
    warn      double precision NOT NULL,
    critical  double precision NOT NULL
);
INSERT INTO thresholds VALUES
    ('temperature_c', 'Temperature', '°C',    35,   40),
    ('humidity',      'Humidity',    '%',     80,   90),
    ('pm25',          'PM2.5',       'µg/m³', 35.5, 55.5),
    ('pm10',          'PM10',        'µg/m³', 155,  255),
    ('co2',           'CO₂',         'ppm',   1000, 2000),
    ('aqi',           'AQI',         '',      101,  151)
ON CONFLICT (metric) DO NOTHING;

-- Rolling-window aggregates. Real-time aggregation is on, so the newest
-- bucket is computed from raw rows until the policy materialises it.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 minute', ts) AS bucket, sensor_id, sensor_group,
       avg(temperature_c) AS temperature_c, avg(humidity) AS humidity, avg(pressure_hpa) AS pressure_hpa,
       avg(pm25) AS pm25, max(pm25) AS pm25_max, avg(pm10) AS pm10, avg(co2) AS co2,
       avg(aqi) AS aqi, max(aqi) AS aqi_max,
       count(*) AS n, count(*) FILTER (WHERE cardinality(anomalies) > 0) AS anomaly_count
FROM readings GROUP BY bucket, sensor_id, sensor_group
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_15m
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('15 minutes', ts) AS bucket, sensor_id, sensor_group,
       avg(temperature_c) AS temperature_c, avg(humidity) AS humidity, avg(pressure_hpa) AS pressure_hpa,
       avg(pm25) AS pm25, max(pm25) AS pm25_max, avg(pm10) AS pm10, avg(co2) AS co2,
       avg(aqi) AS aqi, max(aqi) AS aqi_max,
       count(*) AS n, count(*) FILTER (WHERE cardinality(anomalies) > 0) AS anomaly_count
FROM readings GROUP BY bucket, sensor_id, sensor_group
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS readings_1h
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 hour', ts) AS bucket, sensor_id, sensor_group,
       avg(temperature_c) AS temperature_c, avg(humidity) AS humidity, avg(pressure_hpa) AS pressure_hpa,
       avg(pm25) AS pm25, max(pm25) AS pm25_max, avg(pm10) AS pm10, avg(co2) AS co2,
       avg(aqi) AS aqi, max(aqi) AS aqi_max,
       count(*) AS n, count(*) FILTER (WHERE cardinality(anomalies) > 0) AS anomaly_count
FROM readings GROUP BY bucket, sensor_id, sensor_group
WITH NO DATA;

SELECT add_continuous_aggregate_policy('readings_1m',  start_offset => INTERVAL '1 hour',
       end_offset => INTERVAL '1 minute',  schedule_interval => INTERVAL '30 seconds', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('readings_15m', start_offset => INTERVAL '6 hours',
       end_offset => INTERVAL '15 minutes', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
SELECT add_continuous_aggregate_policy('readings_1h',  start_offset => INTERVAL '3 days',
       end_offset => INTERVAL '1 hour',    schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- BI layer: the only objects Power BI needs to see.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS bi;

CREATE OR REPLACE VIEW bi.dim_sensor AS
SELECT sensor_id, sensor_group, first_seen, last_seen,
       (now() - last_seen) < INTERVAL '2 minutes' AS is_online
FROM sensors;

CREATE OR REPLACE VIEW bi.dim_threshold AS SELECT * FROM thresholds;

CREATE OR REPLACE VIEW bi.latest_by_sensor AS
SELECT DISTINCT ON (r.sensor_id)
       r.sensor_id, r.sensor_group, r.ts, r.temperature_c, r.humidity, r.pressure_hpa,
       r.pm25, r.pm10, r.co2, r.aqi, r.aqi_category,
       cardinality(r.anomalies) > 0 AS is_anomaly, r.anomalies
FROM readings r
WHERE r.ts > now() - INTERVAL '10 minutes'
ORDER BY r.sensor_id, r.ts DESC;

CREATE OR REPLACE VIEW bi.fact_1m  AS SELECT * FROM readings_1m  WHERE bucket > now() - INTERVAL '24 hours';
CREATE OR REPLACE VIEW bi.fact_15m AS SELECT * FROM readings_15m WHERE bucket > now() - INTERVAL '7 days';
CREATE OR REPLACE VIEW bi.fact_1h  AS SELECT * FROM readings_1h;

-- One row of headline KPIs over the last 5 minutes.
CREATE OR REPLACE VIEW bi.kpi_now AS
SELECT now() AS as_of,
       count(DISTINCT sensor_id)                                          AS active_sensors,
       count(*)                                                           AS readings_5m,
       round((count(*) / 300.0)::numeric, 1)                              AS readings_per_s,
       round(avg(temperature_c)::numeric, 2)                              AS avg_temperature_c,
       round(avg(humidity)::numeric, 1)                                   AS avg_humidity,
       round(avg(pressure_hpa)::numeric, 1)                               AS avg_pressure_hpa,
       round(avg(pm25)::numeric, 1)                                       AS avg_pm25,
       round(avg(co2)::numeric, 0)                                        AS avg_co2,
       round(avg(aqi)::numeric, 0)                                        AS avg_aqi,
       max(aqi)                                                           AS max_aqi,
       count(*) FILTER (WHERE cardinality(anomalies) > 0)                 AS anomalies_5m,
       count(*) FILTER (WHERE cardinality(gap_filled) > 0)                AS gap_filled_5m,
       round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)))::numeric, 3) AS latency_p50_s,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)))::numeric, 3) AS latency_p95_s
FROM readings
WHERE ts > now() - INTERVAL '5 minutes';

-- Threshold breaches in the last 24 hours, one row per sensor, metric and minute.
CREATE OR REPLACE VIEW bi.alerts AS
WITH m AS (
    SELECT bucket, sensor_id, sensor_group, metric, value
    FROM readings_1m,
         LATERAL (VALUES ('temperature_c', temperature_c), ('humidity', humidity),
                         ('pm25', pm25), ('pm10', pm10), ('co2', co2), ('aqi', aqi::double precision)) v(metric, value)
    WHERE bucket > now() - INTERVAL '24 hours'
)
SELECT m.bucket, m.sensor_id, m.sensor_group, m.metric, t.label, t.unit, round(m.value::numeric, 2) AS value,
       CASE WHEN m.value >= t.critical THEN 'critical' ELSE 'warning' END AS severity
FROM m JOIN thresholds t USING (metric)
WHERE m.value >= t.warn;

-- Individual anomalous readings flagged by the EWMA detector.
CREATE OR REPLACE VIEW bi.anomalies AS
SELECT ts, sensor_id, sensor_group, unnest(anomalies) AS metric, pm25, pm10, co2, temperature_c, humidity, aqi
FROM readings
WHERE cardinality(anomalies) > 0 AND ts > now() - INTERVAL '24 hours';

-- Pipeline health per minute, for the operations page of the report.
CREATE OR REPLACE VIEW bi.pipeline_1m AS
SELECT time_bucket('1 minute', processed_ts) AS minute,
       count(*) AS records,
       round((count(*) / 60.0)::numeric, 1) AS records_per_s,
       round((percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)))::numeric, 3) AS latency_p50_s,
       round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)))::numeric, 3) AS latency_p95_s,
       round((percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)))::numeric, 3) AS latency_p99_s
FROM readings
WHERE processed_ts > now() - INTERVAL '6 hours'
GROUP BY 1;

-- Read-only login for Power BI.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi') THEN
        CREATE ROLE powerbi LOGIN PASSWORD 'powerbi';
    END IF;
END $$;
GRANT USAGE ON SCHEMA bi TO powerbi;
GRANT SELECT ON ALL TABLES IN SCHEMA bi TO powerbi;
GRANT SELECT ON readings, sensors, thresholds, readings_1m, readings_15m, readings_1h TO powerbi;
