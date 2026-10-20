"""Scalability benchmark: Kubernetes deployment vs the Docker Compose baseline.

Two scenarios, each run against whichever stack is currently up:

load      steps the offered load through --rates (msg/s) and records, per step,
          achieved throughput, end-to-end latency percentiles (sensor
          timestamp -> database commit), peak stream backlog, delivery ratio
          and the number of processor replicas.

recovery  holds a steady load, kills every process worker part-way through,
          and measures the outage, the time to recover, the time to drain the
          backlog, and whether any reading was lost.

Results go to bench/results/<target>_<scenario>_<time>.json; bench/report.py
turns them into tables and charts.

    python bench/run_bench.py --target compose --scenario load
    python bench/run_bench.py --target k8s --scenario recovery --rate 1000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "services" / "sensor-sim" / "sim.py"
RESULTS = ROOT / "bench" / "results"

TARGETS = {
    "compose": {
        "db": "postgresql://envbi:envbi@localhost:5442/envbi",
        "kill": ["docker", "kill", "envbi-process-1"],
        "restore": ["docker", "start", "envbi-process-1"],
        "replicas": None,
    },
    "k8s": {
        # The read replica is published on 5433; latency columns are written
        # by the workers, so replica lag does not distort them.
        "db": "postgresql://envbi:envbi@localhost:5433/envbi",
        # Force-delete with no grace period: a crash, like `docker kill` above.
        "kill": ["kubectl", "-n", "envbi", "delete", "pod", "-l", "app=process", "--grace-period=0", "--force", "--wait=false"],
        "restore": None,
        "replicas": ["kubectl", "-n", "envbi", "get", "deploy", "process", "-o", "jsonpath={.status.readyReplicas}"],
    },
}
GATEWAY = "http://localhost:8000/api/kpis"
MAX_RATE_PER_SIM = 800  # one simulator process comfortably sustains this


def start_sims(rate: float, duration: float, sensors: int, seed: int) -> list[subprocess.Popen]:
    n = max(1, math.ceil(rate / MAX_RATE_PER_SIM))
    per_proc_sensors = max(1, sensors // n)
    per_sensor_rate = rate / (per_proc_sensors * n)
    procs = []
    for i in range(n):
        cmd = [sys.executable, "-u", str(SIM), "--sensors", str(per_proc_sensors), "--rate", f"{per_sensor_rate:.4f}",
               "--duration", str(duration), "--malformed-rate", "0", "--seed", str(seed + i),
               "--id-offset", str(i * per_proc_sensors)]
        procs.append(subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
    return procs


def collect_sims(procs) -> int:
    sent = 0
    for p in procs:
        out, _ = p.communicate()
        m = re.search(r"done: (\d+) messages", out or "")
        if m:
            sent += int(m.group(1))
        else:
            print(out, file=sys.stderr)
    return sent


def kpis() -> dict:
    try:
        with urllib.request.urlopen(GATEWAY, timeout=3) as resp:
            return json.load(resp)
    except Exception:
        return {}


def replicas(target: str) -> int | None:
    cmd = TARGETS[target]["replicas"]
    if not cmd:
        return 1
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


async def sample(target: str, stop: asyncio.Event, samples: list, t0: float) -> None:
    while not stop.is_set():
        k = await asyncio.to_thread(kpis)
        rep = await asyncio.to_thread(replicas, target)
        samples.append({"t": round(time.time() - t0, 1), "lag": k.get("stream_lag"),
                        "pending": k.get("stream_pending"), "replicas": rep})
        await asyncio.sleep(2)


async def drain(conn, since: datetime, timeout: float = 180) -> float:
    """Wait until the stream is empty and rows stop arriving."""
    start = time.time()
    last, stable = -1, 0
    while time.time() - start < timeout:
        n = await conn.fetchval("SELECT count(*) FROM readings WHERE ts >= $1", since)
        k = kpis()
        if n == last and not k.get("stream_lag") and not k.get("stream_pending"):
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last = n
        await asyncio.sleep(2)
    return time.time() - start


async def stats(conn, start: datetime, end: datetime) -> dict:
    row = await conn.fetchrow(
        """SELECT count(*) AS rows,
                  percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)) AS p50,
                  percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)) AS p95,
                  percentile_cont(0.99) WITHIN GROUP (ORDER BY extract(epoch FROM processed_ts - ts)) AS p99,
                  max(extract(epoch FROM processed_ts - ts)) AS max
           FROM readings WHERE ts >= $1 AND ts < $2""",
        start, end,
    )
    return {k: (float(v) if v is not None else None) for k, v in dict(row).items()}


async def scenario_load(args, conn) -> dict:
    steps = []
    for rate in args.rates:
        print(f"\n== offered load {rate} msg/s for {args.step}s")
        start = datetime.now(timezone.utc)
        t0 = time.time()
        stop, samples = asyncio.Event(), []
        sampler = asyncio.create_task(sample(args.target, stop, samples, t0))
        procs = start_sims(rate, args.step, args.sensors, args.seed)
        sent = await asyncio.to_thread(collect_sims, procs)
        end = datetime.now(timezone.utc)
        drain_s = await drain(conn, start)
        stop.set()
        await sampler
        s = await stats(conn, start, end)
        step = {
            "offered_rate": rate,
            "sent": sent,
            "stored": int(s["rows"]),
            "delivery_ratio": round(s["rows"] / sent, 4) if sent else None,
            "throughput": round(s["rows"] / args.step, 1),
            "latency_p50_s": s["p50"], "latency_p95_s": s["p95"], "latency_p99_s": s["p99"], "latency_max_s": s["max"],
            "peak_backlog": max((x["lag"] or 0) + (x["pending"] or 0) for x in samples) if samples else None,
            "max_replicas": max((x["replicas"] or 0) for x in samples) if samples else None,
            "drain_s": round(drain_s, 1),
            "samples": samples,
        }
        print({k: v for k, v in step.items() if k != "samples"})
        steps.append(step)
        await asyncio.sleep(args.cooldown)
    return {"steps": steps}


async def scenario_recovery(args, conn) -> dict:
    duration = args.kill_at + args.after
    print(f"steady {args.rate} msg/s for {duration}s, killing workers at t={args.kill_at}s")
    start = datetime.now(timezone.utc)
    t0 = time.time()
    stop, samples = asyncio.Event(), []
    sampler = asyncio.create_task(sample(args.target, stop, samples, t0))
    procs = start_sims(args.rate, duration, args.sensors, args.seed)

    await asyncio.sleep(args.kill_at)
    kill_wall = time.time()
    subprocess.run(TARGETS[args.target]["kill"], check=False, capture_output=True)
    print("workers killed")

    sent = await asyncio.to_thread(collect_sims, procs)
    end = datetime.now(timezone.utc)

    restored_manually = False
    restore = TARGETS[args.target]["restore"]
    # The baseline has no orchestrator: nothing restarts the worker. Record
    # that, then restart it by hand so the backlog can be checked for loss.
    per_second = await conn.fetch(
        "SELECT floor(extract(epoch FROM processed_ts))::bigint AS s, count(*) AS n FROM readings "
        "WHERE ts >= $1 GROUP BY 1 ORDER BY 1", start,
    )
    if restore:
        subprocess.run(restore, check=False, capture_output=True)
        restored_manually = True
    drain_s = await drain(conn, start, timeout=300)
    stop.set()
    await sampler

    counts = {int(r["s"]): int(r["n"]) for r in per_second}
    kill_s = int(kill_wall)
    threshold = 0.5 * args.rate
    # Outage: from the kill until commits climb back above half the offered rate.
    recovered_at = next((s for s in range(kill_s + 1, int(time.time())) if counts.get(s, 0) >= threshold), None)
    stored = await conn.fetchval("SELECT count(*) FROM readings WHERE ts >= $1 AND ts < $2", start, end)
    timeline = [{"t": s - int(t0), "committed": counts.get(s, 0)} for s in range(int(t0), int(time.time()))]
    result = {
        "rate": args.rate,
        "kill_at_s": args.kill_at,
        "sent": sent,
        "stored": int(stored),
        "lost": max(0, sent - int(stored)),
        "recovered": recovered_at is not None,
        "recovery_s": (recovered_at - kill_wall) if recovered_at else None,
        "restored_manually": restored_manually,
        "drain_after_run_s": round(drain_s, 1),
        "peak_backlog": max((x["lag"] or 0) + (x["pending"] or 0) for x in samples) if samples else None,
        "timeline": timeline,
        "samples": samples,
    }
    print({k: v for k, v in result.items() if k not in ("timeline", "samples")})
    return result


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=list(TARGETS), required=True)
    p.add_argument("--scenario", choices=["load", "recovery"], default="load")
    p.add_argument("--rates", type=lambda s: [int(x) for x in s.split(",")], default=[200, 500, 1000, 2000, 3000])
    p.add_argument("--step", type=int, default=60, help="seconds per load step")
    p.add_argument("--cooldown", type=int, default=10)
    p.add_argument("--rate", type=int, default=1000, help="recovery scenario load")
    p.add_argument("--kill-at", type=int, default=40)
    p.add_argument("--after", type=int, default=80)
    p.add_argument("--sensors", type=int, default=400)
    p.add_argument("--seed", type=int, default=100)
    args = p.parse_args()

    conn = await asyncpg.connect(TARGETS[args.target]["db"])
    started = datetime.now(timezone.utc)
    if args.scenario == "load":
        result = await scenario_load(args, conn)
    else:
        result = await scenario_recovery(args, conn)
    await conn.close()

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.target}_{args.scenario}_{started:%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"target": args.target, "scenario": args.scenario, "started": started.isoformat(),
                               "args": vars(args), **result}, indent=2, default=str))
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
