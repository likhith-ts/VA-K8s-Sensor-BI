# Live demo guide

How to bring the project back up for a demo, what to show, and what to say.

## 1. Start (about 2 minutes)

1. **Open Docker Desktop** and wait until it shows *Kubernetes running*
   (green). Check from a terminal:

   ```bash
   kubectl get nodes
   ```

   The node should be `Ready`.

2. **Start everything** from the project folder:

   ```bash
   cd ~/Workspace/Projects/VA-Project-Sem1
   ./deploy/demo/start-demo.sh
   ```

   In about 30 seconds this starts the 11 pods, restores the saved hour of
   sensor data (shifted so it ends "now", so the charts already have full
   curves), starts the sensor simulator in the background (2 hours, 120
   sensors) and opens the dashboard at http://localhost:8000.
   It also prints the Mac's IP address for Power BI.

3. **Power BI (Windows VM):** open `project-dashboard-report.pbix` and click
   *Refresh*. If the IP printed in step 2 is not `192.168.29.239` (for
   example on the college Wi-Fi), first go to *Transform data → Data source
   settings → Change Source*, enter `<printed IP>:5433`, *OK*, *Apply
   changes*, and log in as `powerbi` / `powerbi` if asked.

> Don't run Docker Compose at the same time; both use ports 1883, 8000 and
> 8080. If you skip the restore (`SKIP_RESTORE=1 ./deploy/demo/start-demo.sh`),
> the charts start empty and fill in over the next minutes.

## 2. Stop

```bash
./deploy/demo/stop-demo.sh
```

Then quit Docker Desktop from the menu bar. Data volumes are kept, and the
saved hour in `demo-data/` is reused by the next `start-demo.sh`. To save a
newer hour instead: `./deploy/demo/save-demo-data.sh`.

## 3. Demo script (about 8 minutes)

Keep three windows ready: the browser dashboard, a terminal, and Power BI.

1. **The problem (30 s).** Sensors produce continuous high-volume data;
   traditional single-server setups lose data or fall behind, and BI tools are
   rarely connected to a scalable backend.

2. **The running system (1 min).** In the terminal:

   ```bash
   kubectl -n envbi get pods
   ```

   Point out: 3 ingest pods, 2+ process workers, Redis buffer, TimescaleDB
   primary `tsdb-0` and read replica `tsdb-1`, gateway, MQTT broker.

3. **Live web dashboard (1.5 min).** KPI tiles update every 2 seconds; PM2.5
   by zone; AQI by zone; the sensor field (each square is a sensor, ringed =
   anomaly); throughput and latency (≈ 1–2 ms); threshold alerts; anomalies;
   the dead-letter queue showing rejected bad records and why.

4. **Autoscaling (1.5 min).** Push the load up to about 2,400 readings/s for
   3 minutes:

   ```bash
   for i in 0 1 2; do .venv/bin/python services/sensor-sim/sim.py --sensors 200 --rate 4 --id-offset $((1000 + i*200)) --duration 180 > /dev/null & done
   ```

   Then watch the autoscaler add workers (Ctrl-C to stop watching):

   ```bash
   kubectl -n envbi get hpa,pods -l app=process -w
   ```

   The worker count climbs one step at a time, 2 → 3 → 4 → 5, about every
   30–60 seconds (tested: 5 workers by the end of the 3-minute burst), while
   the dashboard's p95 latency stays in milliseconds. For a bigger climb, use
   `--duration 300`. After the burst it scales back down to 2 within a few
   minutes.

5. **Self-healing (1 min).** Kill every worker as if they crashed:

   ```bash
   kubectl -n envbi delete pod -l app=process --grace-period=0 --force
   ```

   Run `kubectl -n envbi get pods` again: new workers are already starting.
   On the dashboard the backlog tile blips and returns to 0, with no data lost.
   In our benchmark this recovery took about 1 second; without Kubernetes the
   pipeline never recovered on its own.

6. **Power BI (1.5 min).** Overview → Sensor drill-down (pick a zone, then a
   sensor) → Anomalies → Pipeline. Mention it is DirectQuery on the read
   replica, so every click queries live data, and the 27 DAX measures.

7. **Results (30 s).** Show the table in `bench/results/REPORT.md` and the
   deadlock finding.

## 4. What to say

### Aim

Build and evaluate an end-to-end real-time business-intelligence framework
for environmental sensor data that stays fast and reliable as load grows, by
combining containerised microservices orchestrated by Kubernetes with
Power BI analytics.

### Problem statement

IoT sensor networks in smart cities, agriculture and industry continuously
produce high-velocity streams (temperature, humidity, air quality, pressure).
Traditional, single-server data pipelines struggle with this volume: they
delay insights, lose data when something fails, and don't scale. At the same
time, BI tools such as Power BI are usually not integrated with scalable,
containerised backends, so live decision-making from sensor data is hard. Our
literature survey found that existing work either builds dashboards without a
scalable backend, or scales the pipeline without measuring what the dashboard
user experiences. This project builds both together and measures the whole
path.

### How it works, in one minute

Follow a single reading:

1. **Sensor → MQTT broker.** A sensor publishes a JSON reading over MQTT, the
   standard lightweight IoT protocol (QoS 1 = delivered at least once).
2. **Ingest → Redis Stream.** Three ingest pods share the subscription,
   timestamp each reading and append it to a Redis Stream: a durable queue,
   so nothing is lost if processing slows down or crashes. Broken messages go
   to a dead-letter queue.
3. **Process workers.** Validate ranges (e.g. humidity 180% is rejected),
   convert units (°F → °C, kPa → hPa), fill short gaps from the last good
   reading, compute the US EPA Air Quality Index, and flag anomalies with a
   per-sensor running average (z-score > 4). The workers keep no local state,
   so Kubernetes can add or remove them freely.
4. **TimescaleDB.** PostgreSQL extended for time series (fastest in the
   IoTDB benchmark from our survey). It maintains 1-minute, 15-minute and
   1-hour averages automatically. A streaming read replica serves the
   dashboards so analytics never slow down the writes.
5. **Power BI and the web dashboard.** Power BI reads the replica live with
   DirectQuery; the gateway serves the same data to the web dashboard.

**Kubernetes** adds autoscaling (CPU-based HPA, 2–10 workers), self-healing
(crashed pods are replaced), rolling updates and configuration management.

### Results

| | Docker Compose (no orchestration) | Kubernetes |
|---|---|---|
| p95 latency at 3,000 readings/s | 11.8 ms | 6.1 ms |
| p99 latency at 3,000 readings/s | 48.3 ms | 16.4 ms |
| All workers killed | never recovered on its own | recovered in ~1 s |
| Data lost | 0 | 0 |

The benchmark also caught a real bug: with 10 workers, database deadlocks
silently stopped all of them while Kubernetes still showed them as healthy.
We found and fixed it, which shows why measuring the whole system matters.

### Likely questions

- **Why Redis Streams and not Kafka?** Same guarantees for this scale
  (durable log, consumer groups, replay) with far less overhead on one node;
  Kafka is a drop-in change in the design.
- **Why simulated sensors?** Repeatable tests and controlled faults (missing
  fields, spikes, malformed records). Real devices can publish to the same
  MQTT topics.
- **Why does Compose look fine at normal load?** One worker is enough at
  these rates; the difference shows in tail latency, scaling and recovery.
- **How is data loss prevented?** QoS 1 delivery, a durable stream,
  acknowledgement only after the database commit, and reclaiming of
  messages left by crashed workers.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| `start.sh` says *stack not deployed yet* | Run `./deploy/k8s/deploy.sh` once (builds images, about 3 minutes), then `start-demo.sh`. |
| `kubectl` cannot connect | Docker Desktop or its Kubernetes is not running yet; wait for the green status. |
| Dashboard cards blank | The simulator isn't running: check `tail demo-data/simulator.log`, or rerun `start-demo.sh`. |
| Port already in use | Docker Compose is running: `docker compose -f deploy/compose/docker-compose.yml stop`. |
| Power BI can't connect | The Mac's IP changed: use *Change Source* with the IP that `start-demo.sh` printed; check the VM can reach it (`Test-NetConnection <ip> -Port 5433` in PowerShell). |
| Sensors show offline in Power BI | More than 2 minutes without new data; start the simulator. |
