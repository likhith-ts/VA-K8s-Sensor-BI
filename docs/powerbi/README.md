# Power BI report

The pipeline exposes a read-only `bi` schema in TimescaleDB. Power BI connects
to it with **DirectQuery**, so every visual queries live data and the report
needs no import refresh.

> Power BI Desktop runs on Windows only. On a Mac, use a Windows VM or a lab
> PC on the same network and replace `localhost` with the Mac's IP address.

## Quick start: open the prebuilt report

[`project-dashboard-report.pbix`](project-dashboard-report.pbix) already
contains the model, relationships, all 27 measures and the four report pages
described below. To use it:

1. Start the stack and a data feed (the views only show the last 5 minutes /
   24 hours of data, so the report is blank when nothing is running):

   ```bash
   ./deploy/k8s/deploy.sh
   .venv/bin/python services/sensor-sim/sim.py --sensors 120 --rate 2
   ```

2. Copy the `.pbix` to the Windows machine and open it.
3. It points at `192.168.29.239:5433`, the Mac it was built on. If your
   database is elsewhere: *Transform data → Data source settings → Change
   Source*, enter your server (the Mac's IP from `ipconfig getifaddr en0`
   plus `:5433`), then *Apply changes*.
4. When asked for credentials choose **Database**, `powerbi` / `powerbi`.
   The file ships without saved credentials, so this happens once.
5. Optional: *Format page → Page refresh → on, 5 seconds* on each page.

To rebuild the report pages yourself (for example after changing the
layout in [`build_report.py`](build_report.py)), save a `.pbix` that has the
model and measures from steps 1–2 below, then:

```bash
python3 docs/powerbi/build_report.py model-only.pbix project-dashboard-report.pbix
```

The script writes the pages in Power BI's PBIR JSON format (the default
since 2025) and never touches the semantic model. It replaces the existing
pages and drops saved credentials from the copy.

The rest of this guide is the manual route.

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

These are the pages [`build_report.py`](build_report.py) generates; build
them by hand the same way if you prefer.

**Overview**
- Title text box and a *Zone* slicer (`bi dim_sensor[sensor_group]`, dropdown).
- Seven cards: `Sensors Online`, `Readings per Second`, `Avg AQI`,
  `AQI Category`, `Avg PM2.5`, `Alert Status Label`, `Latency p95 (s)`.
- Line chart: X `bi fact_1m[bucket]`, Y `Avg PM2.5`, Legend
  `bi dim_sensor[sensor_group]`.
- Clustered bar: `sensor_group` by `Avg AQI`, bar colour from the
  `AQI Status Color` measure (*fx → Field value*).
- Table of `bi alerts`, newest first.

**Sensor drill-down**
- *Zone* and *Sensor* slicers.
- Four line charts over `bi fact_1m[bucket]`: `Avg Temperature °C`,
  `Avg Humidity %`, `Avg PM2.5`, `Avg CO2 ppm` (one metric per chart, no
  dual axis).
- Table of `bi latest_by_sensor`, worst AQI first.

**Anomalies**
- Cards: `Anomalous Readings`, `Anomaly Rate`, `Sensors in Breach`,
  `Critical Breaches`.
- Column chart: anomalies by `metric`, split by `sensor_group`.
- Bar chart: anomalies per `sensor_id`, most first.
- Table of `bi anomalies`, newest first.

**Pipeline**
- Cards: readings, gap-filled and anomalies in the last 5 minutes (from
  `bi kpi_now`), `Pipeline Throughput (rec/s)`, `Latency p50 (s)`,
  `Latency p95 (s)`.
- Line charts from `bi pipeline_1m`: `records_per_s`, and
  `latency_p50_s` / `latency_p95_s` / `latency_p99_s` together.

One touch the script leaves to you: a PM2.5 threshold line on the Overview
trend (*Analytics pane → Y-axis constant line → fx →
`PM2.5 Warning Threshold`*).

## 4. Live refresh

With DirectQuery, turn on *Format page → Page refresh → Auto page refresh*
and set it to 5 seconds (Desktop allows any interval; in the Power BI Service
the minimum is set by your admin's capacity settings).

## 5. Publishing to the Power BI Service

*Publish* works, but the online report then shows "Data source credentials
missing": the Service runs in Microsoft's cloud and cannot reach a database
on a private address such as `192.168.x.x`. Entering the password alone does
not help. Two ways round it:

**On-premises data gateway** (keeps the database local)

1. On a Windows machine on the same network (the VM is fine), install the
   *On-premises data gateway* in **standard mode** (personal mode does not
   support DirectQuery), sign in with your Power BI account and register a
   gateway.
2. In the Service: gear icon → *Manage connections and gateways* → *New* →
   *On-premises*. Type **PostgreSQL**, server exactly as in the report
   (e.g. `192.168.29.239:5433`), database `envbi`, *Basic* auth
   `powerbi` / `powerbi`. Turn off encryption if it fails with an SSL error
   (the local database has no TLS).
3. Semantic model → *Settings* → *Gateway and cloud connections* → use the
   gateway and map the data source to that connection → *Apply*.

The Mac (stack and feed) and the gateway machine must both be on whenever
the report is viewed, and the Mac's IP must not change (reserve it in the
router).

**Cloud-hosted database** (no gateway): host PostgreSQL somewhere public with
TLS and point *Change Source* at it. Note that most managed Postgres services
ship only the Apache edition of TimescaleDB, which lacks the continuous
aggregates in [`db/init.sql`](../../db/init.sql); those views would need to
become plain `time_bucket` views.

## 6. Optional: push streaming dataset

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

## 7. Alternative: Web connector (Import mode)

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
