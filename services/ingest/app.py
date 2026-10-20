"""Ingestion service: MQTT bridge and HTTP endpoint into the stream buffer.

Readings arriving over MQTT (shared subscription, so replicas split the load)
or HTTP POST are stamped with an ingest timestamp and appended to the Redis
raw stream. Records that are not valid JSON go straight to the dead-letter
stream; deeper validation happens in the process service.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import aiomqtt
import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from envbi_common import config

log = logging.getLogger("ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

RECEIVED = Counter("ingest_messages_total", "Messages received", ["transport"])
REJECTED = Counter("ingest_rejected_total", "Messages sent to the DLQ at ingest", ["reason"])
MQTT_UP = Gauge("ingest_mqtt_connected", "1 when the MQTT bridge is connected")

r: redis.Redis | None = None
# Messages are appended in small batches to cut Redis round trips.
queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=50_000)


async def flusher() -> None:
    while True:
        first = await queue.get()
        batch = [first]
        while len(batch) < 500 and not queue.empty():
            batch.append(queue.get_nowait())
        pipe = r.pipeline(transaction=False)
        for stream, payload in batch:
            maxlen = config.STREAM_MAXLEN if stream == config.RAW_STREAM else 100_000
            pipe.xadd(stream, {"payload": payload}, maxlen=maxlen, approximate=True)
        try:
            await pipe.execute()
        except Exception:
            log.exception("redis write failed, retrying batch of %d", len(batch))
            for item in batch:
                await queue.put(item)
            await asyncio.sleep(1)


async def accept(body: bytes | str, transport: str, topic: str | None = None) -> bool:
    RECEIVED.labels(transport).inc()
    ingest_ts = int(time.time() * 1000)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError as exc:
        REJECTED.labels("bad_json").inc()
        raw = body.decode(errors="replace") if isinstance(body, bytes) else body
        dlq = {"reason": f"bad json: {exc}", "stage": "ingest", "raw": raw, "topic": topic, "ingest_ts": ingest_ts}
        await queue.put((config.DLQ_STREAM, json.dumps(dlq)))
        return False
    data["ingest_ts"] = ingest_ts
    await queue.put((config.RAW_STREAM, json.dumps(data)))
    return True


async def mqtt_bridge() -> None:
    while True:
        try:
            async with aiomqtt.Client(
                config.MQTT_HOST, config.MQTT_PORT, identifier=f"ingest-{config.HOSTNAME}", clean_session=True
            ) as client:
                await client.subscribe(config.MQTT_TOPIC, qos=config.MQTT_QOS)
                MQTT_UP.set(1)
                log.info("subscribed to %s on %s:%s", config.MQTT_TOPIC, config.MQTT_HOST, config.MQTT_PORT)
                async for message in client.messages:
                    await accept(message.payload, "mqtt", str(message.topic))
        except aiomqtt.MqttError as exc:
            MQTT_UP.set(0)
            log.warning("mqtt connection lost (%s), reconnecting in 2s", exc)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global r
    r = redis.from_url(config.REDIS_URL)
    tasks = [asyncio.create_task(flusher()), asyncio.create_task(mqtt_bridge())]
    yield
    for t in tasks:
        t.cancel()
    await r.aclose()


app = FastAPI(title="envbi ingest", lifespan=lifespan)


@app.post("/ingest")
async def ingest(request: Request):
    body = await request.body()
    # Accept a single reading or a JSON array of readings.
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        ok = 0
        for item in parsed:
            ok += await accept(json.dumps(item), "http")
        return {"accepted": ok, "rejected": len(parsed) - ok}
    if await accept(body, "http"):
        return {"accepted": 1}
    return Response(status_code=400, content='{"error":"invalid json"}', media_type="application/json")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    await r.ping()
    return {"ok": True, "queued": queue.qsize()}


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
