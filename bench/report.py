"""Build bench/results/REPORT.md and PNG charts from the latest result files.

    python bench/report.py
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
# Reference palette, light mode: categorical slots 1-2 and chart chrome.
STYLE = {
    "compose": {"color": "#2a78d6", "label": "Docker Compose (baseline)"},
    "k8s": {"color": "#eb6834", "label": "Kubernetes + HPA"},
}
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"


def latest(target: str, scenario: str) -> dict | None:
    files = sorted(RESULTS.glob(f"{target}_{scenario}_*.json"))
    return json.loads(files[-1].read_text()) if files else None


def style_axes(ax, title: str, ylabel: str, xlabel: str) -> None:
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="semibold", pad=22)
    ax.set_ylabel(ylabel, color=INK2, fontsize=9)
    ax.set_xlabel(xlabel, color=INK2, fontsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)


def line_chart(series: dict, title: str, ylabel: str, xlabel: str, path: Path, reference=None, marker_x=None,
               dense: bool = False) -> None:
    fig, ax = plt.subplots(figsize=(7, 3.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    style_axes(ax, title, ylabel, xlabel)
    if reference:
        xs = reference
        ax.plot(xs, xs, color=AXIS, linewidth=1, linestyle=(0, (4, 3)))
        ax.annotate("offered load", (xs[-1], xs[-1]), xytext=(4, 0), textcoords="offset points",
                    color=MUTED, fontsize=8, va="center")
    for target, (xs, ys) in series.items():
        s = STYLE[target]
        if dense:
            # Per-second series: no markers, legend only (end labels would collide).
            ax.plot(xs, ys, color=s["color"], linewidth=2, label=s["label"])
            continue
        ax.plot(xs, ys, color=s["color"], linewidth=2, marker="o", markersize=4.5,
                markeredgecolor=SURFACE, markeredgewidth=1.5, label=s["label"])
        ax.annotate(s["label"].split(" (")[0], (xs[-1], ys[-1]), xytext=(6, 0), textcoords="offset points",
                    color=INK2, fontsize=8, va="center")
    if marker_x is not None:
        ax.axvline(marker_x, color=INK2, linewidth=1, linestyle=(0, (2, 2)))
        ax.annotate("all workers killed", (marker_x, 0.62 * ax.get_ylim()[1]), xytext=(6, 0),
                    textcoords="offset points", color=INK2, fontsize=8, va="center")
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK2, loc="lower left" if dense else "upper left",
              bbox_to_anchor=(0, 1.0) if dense else None, ncol=2 if dense else 1)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)


def ms(v):
    return None if v is None else v * 1000


def fmt(v, d=2):
    return "–" if v is None else f"{v:,.{d}f}"


def main() -> None:
    lines = ["# Scalability evaluation", ""]
    loads = {t: latest(t, "load") for t in STYLE}
    loads = {t: r for t, r in loads.items() if r}
    if loads:
        lines += ["## Load sweep", "",
                  "| Stack | Offered (msg/s) | Achieved (msg/s) | Delivered | p50 latency (ms) | p95 (ms) | p99 (ms) | Peak backlog | Worker pods |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for t, r in loads.items():
            for s in r["steps"]:
                lines.append(f"| {STYLE[t]['label']} | {s['offered_rate']:,} | {fmt(s['throughput'], 0)} | "
                             f"{fmt(100 * (s['delivery_ratio'] or 0), 1)}% | {fmt(ms(s['latency_p50_s']), 1)} | "
                             f"{fmt(ms(s['latency_p95_s']), 1)} | {fmt(ms(s['latency_p99_s']), 1)} | "
                             f"{fmt(s['peak_backlog'], 0)} | {s['max_replicas']} |")
        lines.append("")
        delivered_all = all(s["delivery_ratio"] == 1.0 for r in loads.values() for s in r["steps"])
        if delivered_all:
            lines += ["Both stacks committed every message they were sent at every step (achieved throughput = "
                      "offered load), so the comparison is in latency, backlog and how the capacity is provided.", ""]
        line_chart({t: ([s["offered_rate"] for s in r["steps"]], [s["max_replicas"] for s in r["steps"]]) for t, r in loads.items()},
                   "Process workers as load grows", "worker pods (peak)", "offered load (msg/s)",
                   RESULTS / "workers.png")
        line_chart({t: ([s["offered_rate"] for s in r["steps"]], [ms(s["latency_p95_s"]) or 0 for s in r["steps"]]) for t, r in loads.items()},
                   "End-to-end latency, p95", "milliseconds", "offered load (msg/s)", RESULTS / "latency_p95.png")
        lines += ["![Latency p95](latency_p95.png)", "", "![Workers](workers.png)", ""]

    recs = {t: latest(t, "recovery") for t in STYLE}
    recs = {t: r for t, r in recs.items() if r}
    if recs:
        lines += ["## Failure recovery", "",
                  "Every process worker is killed part-way through a steady load.", "",
                  "| Stack | Load (msg/s) | Recovered by itself | Recovery time (s) | Peak backlog | Readings lost |",
                  "|---|---:|---|---:|---:|---:|"]
        for t, r in recs.items():
            lines.append(f"| {STYLE[t]['label']} | {r['rate']:,} | {'yes' if r['recovered'] and not r['restored_manually'] else 'no'} | "
                         f"{fmt(r['recovery_s'], 1)} | {fmt(r['peak_backlog'], 0)} | {r['lost']:,} |")
        lines.append("")
        first = next(iter(recs.values()))
        kill, end = first["kill_at_s"], first["args"]["kill_at"] + first["args"]["after"]
        line_chart({t: ([p["t"] for p in r["timeline"] if p["t"] < end], [p["committed"] for p in r["timeline"] if p["t"] < end])
                    for t, r in recs.items()},
                   "Commits per second through a worker failure", "records / s", "seconds since start",
                   RESULTS / "recovery.png", marker_x=kill, dense=True)
        lines += ["![Recovery](recovery.png)", "",
                  "The baseline has no orchestrator, so nothing restarts the worker; the benchmark restarts it by hand "
                  "afterwards only to confirm the backlog drains without loss. Under Kubernetes the Deployment "
                  "controller replaces the pods and unacknowledged stream entries are claimed by the new workers.", ""]

    lines += [
        "## Test setup", "",
        "- Both stacks ran on one MacBook under Docker Desktop (Linux VM: 10 vCPU, 8 GB), so the "
        "Kubernetes cluster is a single node; the comparison isolates orchestration behaviour, not extra hardware.",
        "- Load came from `services/sensor-sim` processes on the host publishing over MQTT QoS 1 (400 sensors, "
        "≤800 msg/s per simulator process), with missing fields and spikes enabled and malformed records disabled "
        "so every sent message should be stored.",
        "- Latency is sensor timestamp → database commit, read from the stored rows; backlog is Redis stream lag + "
        "pending entries sampled every 2 s.",
        "- Recovery kills every worker with SIGKILL semantics (`docker kill` / `kubectl delete --grace-period=0 --force`).",
        "",
        "## Finding: worker deadlock under horizontal scaling", "",
        "The first Kubernetes run exposed a bug the single-worker baseline could never hit. With the HPA at 10 "
        "pods, workers upserted overlapping rows of the `sensors` table in different orders inside the readings "
        "transaction; PostgreSQL detected 8 deadlocks within 2 s, and because the exception escaped the consume "
        "task silently, every worker stopped reading while its pod still looked healthy. Only the 15-second claim "
        "loops kept data moving, so p99 latency at 3,000 msg/s reached 32.8 s.",
        "",
        "Fixes in `services/process/worker.py`: the sensor upsert moved to its own short transaction, locks rows "
        "in sorted order, and runs at most every 10 s per sensor per worker; transient database/Redis errors are "
        "retried per batch; and if a worker loop ever dies the process exits so Kubernetes restarts it. The CPU "
        "request was raised from 200m to 500m so the HPA scales with real load (it had reached 10 pods at "
        "1,000 msg/s). After the fix p99 at 3,000 msg/s is 16 ms. All figures above are from the fixed code, "
        "for both stacks.",
        "",
    ]
    (RESULTS / "REPORT.md").write_text("\n".join(lines))
    print(f"wrote {RESULTS / 'REPORT.md'}")


if __name__ == "__main__":
    main()
