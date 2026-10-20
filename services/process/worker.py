"""Process service: validate, transform and persist sensor readings.

Consumes the raw Redis stream as part of a consumer group, so any number of
replicas can share the work and the HPA can scale them. For each batch it:

1. validates schema and physical ranges (failures go to the dead-letter stream),
2. normalises units, gap-fills missing fields and derives AQI,
3. scores each metric against a per-sensor EWMA baseline to flag anomalies,
4. writes the batch to TimescaleDB, where continuous aggregates produce the
   1-minute, 15-minute and 1-hour rolling windows,
5. acknowledges the stream entries only after the database commit.

Entries left pending by a crashed worker are claimed by a live one after
CLAIM_IDLE_MS, so a pod restart loses nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as redis
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from envbi_common import config
from envbi_common.pipeline import (
    ValidationError,
    derive,
    detect_anomalies,
    gap_fill,
    last_values,
    validate_and_normalise,
)

log = logging.getLogger("process")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PROCESSED = Counter("process_records_total", "Records written to the store")
DEAD = Counter("process_dlq_total", "Records sent to the dead-letter stream", ["reason"])
DUPLICATES = Counter("process_duplicates_total", "Records dropped as duplicates (QoS-1 redelivery)")
GAP_FILLED = Counter("process_gap_filled_total", "Metric values filled from the last good reading")
ANOMALIES = Counter("process_anomalies_total", "Metric values flagged as anomalous", ["metric"])
BATCH_SECONDS = Histogram("process_batch_seconds", "Time to process one batch")
E2E_LATENCY = Histogram(
    "process_e2e_latency_seconds",
    "Sensor timestamp to database commit",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
RETRIES = Counter("process_batch_retries_total", "Batch attempts that hit a transient error", ["error"])
LAG = Gauge("process_stream_lag", "Entries in the raw stream not yet delivered to the group")
PENDING = Gauge("process_stream_pending", "Entries delivered but not yet acknowledged")

COLUMNS = [
    "ts", "sensor_id", "sensor_group", "seq", "temperature_c", "humidity", "pressure_hpa",
    "pm25", "pm10", "co2", "aqi", "aqi_category", "gap_filled", "anomalies", "ingest_ts", "processed_ts",
]
STATE_KEY = "sensor:state:{}"
STATE_TTL_S = 24 * 3600
# sensors.last_seen only needs to be fresh enough for the 2-minute "online"
# flag, so each worker refreshes a sensor at most this often.
SEEN_REFRESH_S = 10
TRANSIENT = (asyncpg.DeadlockDetectedError, asyncpg.SerializationError, asyncpg.InterfaceError,
             asyncpg.PostgresConnectionError, OSError, redis.ConnectionError, redis.TimeoutError)


class Worker:
    def __init__(self) -> None:
        self.consumer = config.HOSTNAME
        self.stopping = asyncio.Event()
        self.r: redis.Redis | None = None
        self.pool: asyncpg.Pool | None = None
        self.seen: dict[str, float] = {}

    async def start(self) -> None:
        self.r = redis.from_url(config.REDIS_URL, decode_responses=True)
        self.pool = await self._connect_db()
        try:
            await self.r.xgroup_create(config.RAW_STREAM, config.CONSUMER_GROUP, id="0", mkstream=True)
            log.info("created consumer group %s", config.CONSUMER_GROUP)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        log.info("worker %s consuming %s", self.consumer, config.RAW_STREAM)
        await asyncio.gather(self._consume(), self._claim_loop(), self._lag_loop())

    async def _connect_db(self) -> asyncpg.Pool:
        for attempt in range(60):
            try:
                return await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=4)
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("database not ready (%s), retry %d", exc, attempt + 1)
                await asyncio.sleep(2)
        raise RuntimeError("database unavailable")

    async def _consume(self) -> None:
        while not self.stopping.is_set():
            try:
                resp = await self.r.xreadgroup(
                    config.CONSUMER_GROUP, self.consumer, {config.RAW_STREAM: ">"},
                    count=config.BATCH_SIZE, block=config.BLOCK_MS,
                )
            except redis.ConnectionError:
                log.warning("redis unavailable, retrying")
                await asyncio.sleep(1)
                continue
            if resp:
                await self._handle_safely(resp[0][1])

    async def _claim_loop(self) -> None:
        """Take over entries a dead consumer left unacknowledged."""
        while not self.stopping.is_set():
            await asyncio.sleep(config.CLAIM_IDLE_MS / 2000)
            try:
                _, entries, _ = await self.r.xautoclaim(
                    config.RAW_STREAM, config.CONSUMER_GROUP, self.consumer,
                    min_idle_time=config.CLAIM_IDLE_MS, start_id="0-0", count=config.BATCH_SIZE,
                )
            except redis.ConnectionError:
                continue
            entries = [e for e in entries if e[1]]  # trimmed entries come back empty
            if entries:
                log.info("claimed %d stale entries", len(entries))
                await self._handle_safely(entries)

    async def _lag_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                for group in await self.r.xinfo_groups(config.RAW_STREAM):
                    if group["name"] == config.CONSUMER_GROUP:
                        LAG.set(group.get("lag") or 0)
                        PENDING.set(group.get("pending") or 0)
            except redis.ConnectionError:
                pass
            await asyncio.sleep(2)

    async def _handle_safely(self, entries: list) -> None:
        """Process a batch, retrying transient failures.

        A batch that still fails is left unacknowledged, so the claim loop
        (here or in another pod) retries it later; the consume loop carries on.
        """
        for attempt in range(1, 4):
            try:
                await self._handle(entries)
                return
            except TRANSIENT as exc:
                RETRIES.labels(type(exc).__name__).inc()
                log.warning("batch of %d failed (%s), attempt %d", len(entries), exc, attempt)
                await asyncio.sleep(0.2 * attempt)
            except Exception:
                RETRIES.labels("unexpected").inc()
                log.exception("batch of %d failed, leaving it pending", len(entries))
                return
        log.error("batch of %d still failing, leaving it pending for the claim loop", len(entries))

    async def _handle(self, entries: list) -> None:
        started = time.perf_counter()
        ids = [entry_id for entry_id, _ in entries]
        readings, dead = [], []

        for entry_id, fields in entries:
            raw = fields.get("payload", "")
            try:
                reading = validate_and_normalise(json.loads(raw))
                readings.append(reading)
            except (ValidationError, ValueError) as exc:
                reason = str(exc)
                DEAD.labels(reason.split(" ")[0].split("=")[0][:40]).inc()
                dead.append({"reason": reason, "stage": "process", "raw": raw, "entry_id": entry_id})

        # Load state for each sensor in the batch in one round trip.
        sensors = sorted({rd.sensor_id for rd in readings})
        pipe = self.r.pipeline(transaction=False)
        for sid in sensors:
            pipe.hgetall(STATE_KEY.format(sid))
        states = dict(zip(sensors, await pipe.execute()))

        now = datetime.now(timezone.utc)
        rows = []
        for rd in sorted(readings, key=lambda x: x.ts):
            state = states[rd.sensor_id]
            gap_fill(rd, {k[5:]: v for k, v in state.items() if k.startswith("last_")}, config.GAP_FILL_MAX_AGE_S)
            derive(rd)
            model = {k[6:]: v for k, v in state.items() if k.startswith("model_")}
            model = detect_anomalies(rd, model, config.ANOMALY_Z, config.ANOMALY_ALPHA, config.ANOMALY_WARMUP)
            state.update({f"model_{k}": v for k, v in model.items()})
            state.update({f"last_{k}": v for k, v in last_values(rd).items()})
            GAP_FILLED.inc(len(rd.gap_filled))
            for metric in rd.anomalies:
                ANOMALIES.labels(metric).inc()
            rows.append((
                rd.ts, rd.sensor_id, rd.group, rd.seq, rd.temperature_c, rd.humidity, rd.pressure_hpa,
                rd.pm25, rd.pm10, rd.co2, rd.aqi, rd.aqi_category, rd.gap_filled, rd.anomalies, rd.ingest_ts, now,
            ))

        inserted = await self._write(rows, readings) if rows else 0
        committed = time.time()
        for rd in readings:
            E2E_LATENCY.observe(max(0.0, committed - rd.ts.timestamp()))
        PROCESSED.inc(inserted)
        DUPLICATES.inc(len(rows) - inserted)

        pipe = self.r.pipeline(transaction=False)
        for sid in sensors:
            pipe.hset(STATE_KEY.format(sid), mapping=states[sid])
            pipe.expire(STATE_KEY.format(sid), STATE_TTL_S)
        for item in dead:
            pipe.xadd(config.DLQ_STREAM, {"payload": json.dumps(item)}, maxlen=100_000, approximate=True)
        pipe.xack(config.RAW_STREAM, config.CONSUMER_GROUP, *ids)
        await pipe.execute()
        BATCH_SECONDS.observe(time.perf_counter() - started)

    async def _write(self, rows: list[tuple], readings) -> int:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS staging (LIKE readings INCLUDING DEFAULTS) ON COMMIT DELETE ROWS"
                )
                await conn.copy_records_to_table("staging", records=rows, columns=COLUMNS)
                status = await conn.execute(
                    f"INSERT INTO readings ({', '.join(COLUMNS)}) SELECT {', '.join(COLUMNS)} FROM staging "
                    "ON CONFLICT (sensor_id, ts) DO NOTHING"
                )
            await self._touch_sensors(conn, readings)
        return int(status.split()[-1])

    async def _touch_sensors(self, conn, readings) -> None:
        """Refresh the sensor dimension outside the readings transaction.

        Concurrent workers upserting overlapping sensor rows in different
        orders deadlock, so rows are locked in sorted order, each worker only
        touches a sensor every SEEN_REFRESH_S seconds, and the transaction is
        kept short.
        """
        now = time.monotonic()
        latest = {}
        for rd in readings:
            if now - self.seen.get(rd.sensor_id, 0) < SEEN_REFRESH_S:
                continue
            if rd.sensor_id not in latest or rd.ts > latest[rd.sensor_id][1]:
                latest[rd.sensor_id] = (rd.group, rd.ts)
        if not latest:
            return
        await conn.executemany(
            "INSERT INTO sensors (sensor_id, sensor_group, first_seen, last_seen) VALUES ($1, $2, $3, $3) "
            "ON CONFLICT (sensor_id) DO UPDATE SET last_seen = GREATEST(sensors.last_seen, EXCLUDED.last_seen), "
            "sensor_group = EXCLUDED.sensor_group",
            [(sid, g, ts) for sid, (g, ts) in sorted(latest.items())],
        )
        for sid in latest:
            self.seen[sid] = now

async def main() -> None:
    start_http_server(config.METRICS_PORT)
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stopping.set)
    task = asyncio.create_task(worker.start())
    stop = asyncio.create_task(worker.stopping.wait())
    await asyncio.wait([task, stop], return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        # A loop died: exit non-zero so Kubernetes restarts the pod rather
        # than leaving a live-looking worker that consumes nothing.
        log.error("worker loop exited unexpectedly: %r", task.exception())
        raise SystemExit(1)
    # Give the in-flight batch a moment to commit and ack before exiting.
    await asyncio.sleep(1)
    task.cancel()
    log.info("worker %s stopped", worker.consumer)


if __name__ == "__main__":
    asyncio.run(main())
