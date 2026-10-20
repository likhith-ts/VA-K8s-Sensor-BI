"""IoT sensor field simulator.

Publishes temperature, humidity, pressure, PM2.5, PM10 and CO2 readings for a
fleet of nodes over MQTT (QoS 1) or HTTP. To exercise the pipeline it also
emits, at configurable rates:

* nodes that report in Fahrenheit or kPa (unit normalisation),
* readings with fields missing (gap-filling),
* sudden spikes (anomaly detection),
* malformed records (dead-letter queue).

Examples:
    python sim.py --sensors 50 --rate 1
    python sim.py --sensors 200 --rate 5 --duration 120
    python sim.py --transport http --http-url http://localhost:8080/ingest
"""
from __future__ import annotations

import argparse
import json
import math
import random
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass

GROUPS = ["zone-a", "zone-b", "zone-c", "zone-d"]


@dataclass
class Node:
    sensor_id: str
    group: str
    temp_unit: str
    pressure_unit: str
    base_temp: float
    base_hum: float
    base_pm25: float
    base_co2: float
    phase: float
    seq: int = 0


def make_fleet(n: int, rng: random.Random, offset: int = 0) -> list[Node]:
    nodes = []
    for i in range(n):
        nodes.append(
            Node(
                sensor_id=f"node-{offset + i:04d}",
                group=GROUPS[i % len(GROUPS)],
                temp_unit="F" if rng.random() < 0.15 else "C",
                pressure_unit="kPa" if rng.random() < 0.1 else "hPa",
                base_temp=rng.uniform(22, 32),
                base_hum=rng.uniform(40, 75),
                base_pm25=rng.uniform(6, 45),
                base_co2=rng.uniform(420, 700),
                phase=rng.uniform(0, 2 * math.pi),
            )
        )
    return nodes


def reading(node: Node, rng: random.Random, args) -> dict | str:
    node.seq += 1
    now = time.time()
    # A slow daily-like cycle compressed to 10 minutes so trends show up in a demo.
    cycle = math.sin(2 * math.pi * now / 600 + node.phase)

    temp_c = node.base_temp + 3 * cycle + rng.gauss(0, 0.3)
    pm25 = max(0.5, node.base_pm25 * (1 + 0.3 * cycle) + rng.gauss(0, 1.5))
    msg = {
        "sensor_id": node.sensor_id,
        "group": node.group,
        "seq": node.seq,
        "ts": int(now * 1000),
        "temperature": temp_c * 9 / 5 + 32 if node.temp_unit == "F" else temp_c,
        "temperature_unit": node.temp_unit,
        "humidity": min(100, max(0, node.base_hum - 8 * cycle + rng.gauss(0, 1))),
        "pressure": (1012 + 4 * cycle + rng.gauss(0, 0.5)) / (10 if node.pressure_unit == "kPa" else 1),
        "pressure_unit": node.pressure_unit,
        "pm25": pm25,
        "pm10": pm25 * rng.uniform(1.4, 1.9),
        "co2": node.base_co2 + 60 * cycle + rng.gauss(0, 8),
    }

    roll = rng.random()
    if roll < args.malformed_rate:
        kind = rng.choice(["json", "range", "no_id"])
        if kind == "json":
            return '{"sensor_id": "' + node.sensor_id + '", "ts": '  # truncated JSON
        if kind == "range":
            msg["humidity"] = 180.0
        else:
            msg.pop("sensor_id")
    elif roll < args.malformed_rate + args.missing_rate:
        for key in rng.sample(["humidity", "pressure", "co2", "pm10"], k=rng.randint(1, 2)):
            msg.pop(key, None)
    elif roll < args.malformed_rate + args.missing_rate + args.spike_rate:
        msg["pm25"] = min(900, pm25 * rng.uniform(6, 12))
        msg["pm10"] = msg["pm25"] * 1.6

    return msg


class MqttSink:
    def __init__(self, host: str, port: int, qos: int):
        import paho.mqtt.client as mqtt

        self.qos = qos
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{random.randint(0, 1_000_000)}")
        self.client.max_inflight_messages_set(1000)
        self.client.max_queued_messages_set(0)
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

    def send(self, node: Node, payload: str) -> None:
        self.client.publish(f"sensors/{node.group}/{node.sensor_id}", payload, qos=self.qos)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class HttpSink:
    def __init__(self, url: str):
        self.url = url

    def send(self, node: Node, payload: str) -> None:
        req = urllib.request.Request(self.url, data=payload.encode(), headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as exc:  # malformed records are rejected with 4xx by design
            if not getattr(exc, "code", None):
                print(f"http send failed: {exc}", file=sys.stderr)

    def close(self) -> None:
        pass


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sensors", type=int, default=50)
    p.add_argument("--rate", type=float, default=1.0, help="readings per second per sensor")
    p.add_argument("--duration", type=float, default=0, help="seconds to run, 0 = forever")
    p.add_argument("--transport", choices=["mqtt", "http"], default="mqtt")
    p.add_argument("--mqtt-host", default="localhost")
    p.add_argument("--mqtt-port", type=int, default=1883)
    p.add_argument("--qos", type=int, default=1)
    p.add_argument("--http-url", default="http://localhost:8080/ingest")
    p.add_argument("--malformed-rate", type=float, default=0.005)
    p.add_argument("--missing-rate", type=float, default=0.03)
    p.add_argument("--spike-rate", type=float, default=0.004)
    p.add_argument("--seed", type=int, default=17)
    p.add_argument("--id-offset", type=int, default=0, help="first sensor number, to run several simulators side by side")
    args = p.parse_args()

    rng = random.Random(args.seed)
    fleet = make_fleet(args.sensors, rng, args.id_offset)
    sink = MqttSink(args.mqtt_host, args.mqtt_port, args.qos) if args.transport == "mqtt" else HttpSink(args.http_url)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    total_rate = args.sensors * args.rate
    interval = 1.0 / total_rate
    print(f"simulating {args.sensors} sensors at {args.rate}/s each ({total_rate:.0f} msg/s) over {args.transport}")

    start = time.monotonic()
    sent = 0
    next_report = start + 5
    i = 0
    while not stop.is_set():
        now = time.monotonic()
        if args.duration and now - start >= args.duration:
            break
        # Pace against the schedule rather than sleeping a fixed interval so
        # the target rate holds even when sends are slow.
        due = start + sent * interval
        if due > now:
            time.sleep(min(due - now, 0.05))
            continue
        node = fleet[i % len(fleet)]
        i += 1
        msg = reading(node, rng, args)
        sink.send(node, msg if isinstance(msg, str) else json.dumps(msg))
        sent += 1
        if now >= next_report:
            print(f"sent {sent} messages, {sent / (now - start):.0f} msg/s")
            next_report = now + 5

    sink.close()
    elapsed = time.monotonic() - start
    print(f"done: {sent} messages in {elapsed:.1f}s ({sent / max(elapsed, 1e-9):.0f} msg/s)")


if __name__ == "__main__":
    main()
