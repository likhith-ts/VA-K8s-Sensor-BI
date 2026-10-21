# Power BI report

The pipeline exposes a read-only `bi` schema in TimescaleDB. Power BI connects
to it with **DirectQuery**, so every visual queries live data and the report
needs no import refresh.

> Power BI Desktop runs on Windows only. On a Mac, use a Windows VM or a lab
> PC on the same network and replace `localhost` with the Mac's IP address.

## 1. Connect

| Stack | Server | Database | User / password |
|---|---|---|---|
| Kubernetes (read replica) | `localhost:5433` | `envbi` | `powerbi` / `powerbi` |
| Docker Compose | `localhost:5442` | `envbi` | `powerbi` / `powerbi` |

The quickest way is to double-click [`envbi-k8s.pbids`](envbi-k8s.pbids) (or
[`envbi-compose.pbids`](envbi-compose.pbids)). Power BI Desktop opens with the
PostgreSQL connection filled in and DirectQuery selected. Pick **Database**
credentials, enter `powerbi` / `powerbi`, and skip encryption if prompted
(local cluster, no TLS).

Manual route: *Get data → PostgreSQL database*, server and database from the
table, *Data connectivity mode: DirectQuery*.

In the Navigator, tick these views from the `bi` schema:

| View | Grain | Use |
|---|---|---|
| `bi.fact_1m` | sensor × minute, last 24 h | main fact table: trends, averages |
| `bi.fact_15m`, `bi.fact_1h` | sensor × 15 min / hour | longer ranges |
| `bi.dim_sensor` | sensor | zone, online flag, slicers |
| `bi.dim_threshold` | metric | warning / critical lines |
| `bi.latest_by_sensor` | sensor | current-state table and map |
| `bi.kpi_now` | one row | headline cards |
| `bi.alerts` | sensor × metric × minute | threshold breach table |
| `bi.anomalies` | reading | anomaly drill-through |
| `bi.pipeline_1m` | minute | throughput / latency (ops page) |

## 2. Model

*Model view* → create relationships (many-to-one, single direction):

- `bi fact_1m[sensor_id]` → `bi dim_sensor[sensor_id]`
- `bi alerts[sensor_id]` → `bi dim_sensor[sensor_id]`
- `bi anomalies[sensor_id]` → `bi dim_sensor[sensor_id]`
- `bi latest_by_sensor[sensor_id]` → `bi dim_sensor[sensor_id]`

Then add the measures from [`measures.dax`](measures.dax) in one go:

1. Open **DAX query view** (the *DAX* icon in the left rail).
2. Paste the whole file and press **Run**. A one-row result confirms the
   measures evaluate (the database must be running).
3. Click **Update model with changes** above the `DEFINE` block. All 27
   measures appear under `bi fact_1m` in the Data pane.

Don't paste the file into a single *New measure* box: it holds exactly one
measure, so the whole file fails with "The syntax for '[n]' is incorrect".

## 3. Report pages

**Overview**
- Cards: `Sensors Online`, `Readings per Second`, `Avg AQI` (+ `AQI Category`
  as the card's subtitle), `Avg PM2.5`, `Alert Status Label`, `Latency p95 (s)`.
- Line chart: X `bi fact_1m[bucket]`, Y `Avg PM2.5`, Legend
  `bi dim_sensor[sensor_group]`. Add `PM2.5 Warning Threshold` as a constant
  line (*Analytics pane → Y-axis constant line → fx*).
- Clustered bar: Y `sensor_group`, X `Avg AQI`; background colour from
  `AQI Status Color` (*fx → Field value*).
- Table: `bi alerts` (`bucket`, `severity`, `sensor_id`, `label`, `value`),
  sorted by `bucket` descending.
- Slicer: `bi dim_sensor[sensor_group]`.

**Sensor drill-down**
- Slicer on `sensor_id`; small multiples line chart of temperature, humidity,
  PM2.5 and CO₂ from `bi fact_1m` (one panel per metric, no dual axis).
- Matrix of `bi latest_by_sensor` with conditional formatting on `aqi`.

**Anomalies**
- Scatter: X `bi anomalies[ts]`, Y `pm25`, Legend `metric`.
- Table of anomalous readings; drill-through from Overview by `sensor_id`.

**Pipeline**
- Line charts from `bi pipeline_1m`: `records_per_s` on one chart,
  `latency_p50_s` / `latency_p95_s` / `latency_p99_s` on another.
- Cards from `bi kpi_now`: `readings_5m`, `gap_filled_5m`, `anomalies_5m`.

## 4. Live refresh

With DirectQuery, turn on *Format page → Page refresh → Auto page refresh*
and set it to 5 seconds (Desktop allows any interval; in the Power BI Service
the minimum is set by your admin's capacity settings).

## 5. Optional: push streaming dataset

For a sub-second tile in the Power BI Service without a gateway:

1. In the Service, *My workspace → New → Streaming dataset → API*. Add these
   fields: `as_of` (DateTime), `active_sensors`, `readings_per_s`,
   `avg_temperature_c`, `avg_humidity`, `avg_pm25`, `avg_co2`, `avg_aqi`,
   `max_aqi`, `anomalies_5m`, `latency_p95_s`, `stream_lag`, `dlq_size`
   (Number). Turn on *Historic data analysis*.
2. Copy the *Push URL*.
3. Give it to the gateway:
   - Compose: `POWERBI_PUSH_URL='<url>' docker compose -f deploy/compose/docker-compose.yml up -d gateway`
   - Kubernetes: `kubectl -n envbi create secret generic powerbi-push --from-literal=url='<url>' && kubectl -n envbi rollout restart deploy/gateway`
4. Pin a dashboard tile from the streaming dataset. The gateway posts every 5 s
   (`POWERBI_PUSH_INTERVAL_S`).

## 6. Alternative: Web connector (Import mode)

If PostgreSQL is not reachable, the gateway's JSON API works with
*Get data → Web*:

```powerquery
let
    Source = Json.Document(Web.Contents("http://localhost:8000/api/series?metric=pm25&window=1m&hours=6&by=sensor")),
    Table = Table.FromRecords(Source),
    Typed = Table.TransformColumnTypes(Table, {{"bucket", type datetimezone}, {"value", type number}})
in
    Typed
```

Other endpoints: `/api/kpis`, `/api/latest`, `/api/groups`, `/api/alerts`,
`/api/anomalies`, `/api/pipeline`, `/api/thresholds` (see
`http://localhost:8000/docs`). Import mode needs scheduled refresh.
