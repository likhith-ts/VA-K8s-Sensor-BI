"""Serving gateway: curated views over REST for Power BI and the live dashboard.

Power BI can reach the data in three ways:

* DirectQuery straight to the PostgreSQL ``bi`` schema (recommended),
* the Web connector against the JSON endpoints below (scheduled refresh),
* push: when POWERBI_PUSH_URL is set to a streaming dataset's push URL, the
  gateway posts the headline KPIs every POWERBI_PUSH_INTERVAL_S seconds.

The same endpoints feed the bundled dashboard at ``/`` for demos on machines
without Power BI Desktop.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from envbi_common import config

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

PUSHES = Counter("gateway_powerbi_push_total", "Pushes to the Power BI streaming dataset", ["status"])
STATIC = Path(__file__).parent / "static"
METRICS = {"temperature_c", "humidity", "pressure_hpa", "pm25", "pm10", "co2", "aqi"}
WINDOWS = {"1m": "readings_1m", "15m": "readings_15m", "1h": "readings_1h"}

pool: asyncpg.Pool | None = None
r: redis.Redis | None = None


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def rows(records) -> list[dict]:
    return [{k: _jsonable(v) for k, v in rec.items()} for rec in records]


async def fetch(sql: str, *args) -> list[dict]:
    async with pool.acquire() as conn:
        return rows(await conn.fetch(sql, *args))


async def kpis() -> dict:
    out = (await fetch("SELECT * FROM bi.kpi_now"))[0]
    out.update(await pipeline_state())
    return out


async def pipeline_state() -> dict:
    state = {"stream_lag": None, "stream_pending": None, "dlq_size": None, "consumers": None}
    try:
        state["dlq_size"] = await r.xlen(config.DLQ_STREAM)
        for group in await r.xinfo_groups(config.RAW_STREAM):
            if group["name"] == config.CONSUMER_GROUP:
                state.update(stream_lag=group.get("lag"), stream_pending=group.get("pending"),
                             consumers=group.get("consumers"))
    except redis.RedisError:
        pass
    return state


async def powerbi_pusher() -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            await asyncio.sleep(config.POWERBI_PUSH_INTERVAL_S)
            try:
                payload = await kpis()
                payload["as_of"] = datetime.now().astimezone().isoformat()
                resp = await client.post(config.POWERBI_PUSH_URL, json=[payload])
                PUSHES.labels(str(resp.status_code)).inc()
            except Exception as exc:
                PUSHES.labels("error").inc()
                log.warning("power bi push failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool, r
    for attempt in range(60):
        try:
            pool = await asyncpg.create_pool(config.READ_DATABASE_URL, min_size=1, max_size=8)
            break
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("database not ready (%s), retry %d", exc, attempt + 1)
            await asyncio.sleep(2)
    r = redis.from_url(config.REDIS_URL, decode_responses=True)
    task = asyncio.create_task(powerbi_pusher()) if config.POWERBI_PUSH_URL else None
    yield
    if task:
        task.cancel()
    await pool.close()
    await r.aclose()


app = FastAPI(title="envbi gateway", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def index():
    # Assets are versioned in the HTML, so only the page itself must not be cached.
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/kpis")
async def api_kpis():
    return await kpis()


@app.get("/api/latest")
async def api_latest(group: str | None = None):
    if group:
        return await fetch("SELECT * FROM bi.latest_by_sensor WHERE sensor_group = $1 ORDER BY sensor_id", group)
    return await fetch("SELECT * FROM bi.latest_by_sensor ORDER BY sensor_id")


@app.get("/api/groups")
async def api_groups():
    return await fetch(
        """SELECT sensor_group, count(*) AS sensors,
                  round(avg(temperature_c)::numeric, 2) AS temperature_c, round(avg(humidity)::numeric, 1) AS humidity,
                  round(avg(pm25)::numeric, 1) AS pm25, round(avg(co2)::numeric, 0) AS co2,
                  round(avg(aqi)::numeric, 0) AS aqi, max(aqi) AS max_aqi,
                  count(*) FILTER (WHERE is_anomaly) AS anomalous_sensors
           FROM bi.latest_by_sensor GROUP BY sensor_group ORDER BY sensor_group"""
    )


@app.get("/api/series")
async def api_series(
    metric: str = "pm25",
    window: str = "1m",
    hours: float = Query(1, gt=0, le=24 * 30),
    by: str = Query("group", pattern="^(group|sensor|all)$"),
    sensor_id: str | None = None,
):
    if metric not in METRICS or window not in WINDOWS:
        raise HTTPException(400, f"metric must be one of {sorted(METRICS)}, window one of {sorted(WINDOWS)}")
    view = WINDOWS[window]
    # metric and view are checked against allow-lists above, so formatting them in is safe.
    if by == "all":
        sql = f"SELECT bucket, avg({metric}) AS value FROM {view} WHERE bucket > now() - $1::interval GROUP BY 1 ORDER BY 1"
        return await fetch(sql, timedelta(hours=hours))
    key = "sensor_id" if by == "sensor" else "sensor_group"
    where = "AND sensor_id = $2" if sensor_id else ""
    sql = (f"SELECT bucket, {key} AS series, avg({metric}) AS value FROM {view} "
           f"WHERE bucket > now() - $1::interval {where} GROUP BY 1, 2 ORDER BY 1")
    args = [timedelta(hours=hours)] + ([sensor_id] if sensor_id else [])
    return await fetch(sql, *args)


@app.get("/api/alerts")
async def api_alerts(minutes: int = Query(60, gt=0, le=1440), limit: int = Query(200, le=5000)):
    return await fetch(
        "SELECT * FROM bi.alerts WHERE bucket > now() - $1::interval ORDER BY bucket DESC, severity LIMIT $2",
        timedelta(minutes=minutes), limit,
    )


@app.get("/api/anomalies")
async def api_anomalies(minutes: int = Query(60, gt=0, le=1440), limit: int = Query(200, le=5000)):
    return await fetch(
        "SELECT * FROM bi.anomalies WHERE ts > now() - $1::interval ORDER BY ts DESC LIMIT $2",
        timedelta(minutes=minutes), limit,
    )


@app.get("/api/pipeline")
async def api_pipeline(hours: float = Query(1, gt=0, le=6)):
    series = await fetch("SELECT * FROM bi.pipeline_1m WHERE minute > now() - $1::interval ORDER BY minute",
                         timedelta(hours=hours))
    return {"state": await pipeline_state(), "series": series}


@app.get("/api/dlq")
async def api_dlq(limit: int = Query(50, le=1000)):
    entries = await r.xrevrange(config.DLQ_STREAM, count=limit)
    return [{"id": eid, **json.loads(fields["payload"])} for eid, fields in entries]


@app.get("/api/thresholds")
async def api_thresholds():
    return await fetch("SELECT * FROM thresholds ORDER BY metric")


@app.get("/api/stream")
async def api_stream(interval: float = Query(2, ge=0.5, le=60)):
    """Server-sent events with the KPI row, for push-style dashboard refresh."""
    async def gen():
        while True:
            try:
                yield f"data: {json.dumps(await kpis(), default=_jsonable)}\n\n"
            except Exception as exc:
                yield f"event: error\ndata: {json.dumps(str(exc))}\n\n"
            await asyncio.sleep(interval)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
