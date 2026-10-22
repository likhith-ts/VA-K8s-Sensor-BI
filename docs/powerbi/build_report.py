"""Add the report pages from docs/powerbi/README.md to a .pbix.

The .pbix must use the PBIR report format (Report/definition/... inside the
archive), which Power BI Desktop uses by default since 2025. The semantic
model (tables, relationships, the measures from measures.dax) is left
untouched; only report pages and visuals are written.

    python docs/powerbi/build_report.py project-dashboard.pbix project-dashboard-report.pbix

The existing pages are replaced by four pages: Overview, Sensor drill-down,
Anomalies and Pipeline. SecurityBindings is dropped from the copy, so Power
BI asks for the database credentials once when the file is opened.
"""
from __future__ import annotations

import hashlib
import json
import sys
import zipfile

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"
VISUAL_SCHEMA = f"{SCHEMA}/visualContainer/2.9.0/schema.json"
PAGE_SCHEMA = f"{SCHEMA}/page/2.1.0/schema.json"
PAGES_SCHEMA = f"{SCHEMA}/pagesMetadata/1.1.0/schema.json"

FACT = "bi fact_1m"
W, H, M, GAP = 1920, 1080, 24, 16


# ---- query expression helpers ------------------------------------------------
def _src(table):
    return {"SourceRef": {"Entity": table}}


def col(table, name):
    return {"field": {"Column": {"Expression": _src(table), "Property": name}},
            "queryRef": f"{table}.{name}", "nativeQueryRef": name}


def measure(name, table=FACT, fmt=None, display=None):
    p = {"field": {"Measure": {"Expression": _src(table), "Property": name}},
         "queryRef": f"{table}.{name}", "nativeQueryRef": name}
    if fmt:
        p["format"] = fmt
    if display:
        p["displayName"] = display
    return p


AGG = {"sum": (0, "Sum"), "avg": (1, "Avg"), "max": (4, "Max"), "count": (5, "CountNonNull")}


def agg(table, name, how, display=None, fmt=None):
    fn, label = AGG[how]
    p = {"field": {"Aggregation": {"Expression": {"Column": {"Expression": _src(table), "Property": name}},
                                   "Function": fn}},
         "queryRef": f"{label}({table}.{name})", "nativeQueryRef": f"{label} of {name}"}
    if display:
        p["displayName"] = display
    if fmt:
        p["format"] = fmt
    return p


def lit(value):
    return {"expr": {"Literal": {"Value": value}}}


def text(s):
    return lit("'" + s.replace("'", "''") + "'")


def color(hex_):
    return {"solid": {"color": text(hex_)}}


# ---- visual builders ---------------------------------------------------------
def _name(page, key):
    return hashlib.sha1(f"{page}/{key}".encode()).hexdigest()[:20]


class Page:
    def __init__(self, key, display):
        self.key, self.display = key, display
        self.name = _name("page", key)
        self.visuals = []

    def add(self, key, x, y, w, h, visual, title=None):
        container = {
            "$schema": VISUAL_SCHEMA,
            "name": _name(self.key, key),
            "position": {"x": x, "y": y, "z": len(self.visuals) * 1000, "width": w, "height": h,
                         "tabOrder": len(self.visuals) * 1000},
            "visual": visual,
        }
        if title is not None:
            visual.setdefault("visualContainerObjects", {})["title"] = [
                {"properties": {"show": lit("true"), "text": text(title)}}]
        self.visuals.append(container)


def textbox(content, size="20pt", sub=None):
    paras = [{"textRuns": [{"value": content, "textStyle": {"fontWeight": "bold", "fontSize": size}}]}]
    if sub:
        paras.append({"textRuns": [{"value": sub, "textStyle": {"fontSize": "11pt", "color": "#52514e"}}]})
    return {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paras}}]},
            "drillFilterOtherVisuals": True}


def card(projection):
    return {"visualType": "card", "query": {"queryState": {"Values": {"projections": [projection]}}},
            "drillFilterOtherVisuals": True}


def slicer(projection, dropdown=True):
    v = {"visualType": "slicer", "query": {"queryState": {"Values": {"projections": [projection]}}},
         "drillFilterOtherVisuals": True}
    if dropdown:
        v["objects"] = {"data": [{"properties": {"mode": text("Dropdown")}}]}
    return v


def chart(kind, category, values, series=None, sort=None, objects=None):
    state = {"Category": {"projections": [category]}, "Y": {"projections": values}}
    if series:
        state["Series"] = {"projections": [series]}
    q = {"queryState": state}
    if sort:
        q["sortDefinition"] = {"sort": sort, "isDefaultSort": False}
    v = {"visualType": kind, "query": q, "drillFilterOtherVisuals": True}
    if objects:
        v["objects"] = objects
    return v


def table(projections, sort=None):
    q = {"queryState": {"Values": {"projections": projections}}}
    if sort:
        q["sortDefinition"] = {"sort": sort, "isDefaultSort": False}
    return {"visualType": "tableEx", "query": q, "drillFilterOtherVisuals": True}


def sort_desc(projection):
    return [{"field": projection["field"], "direction": "Descending"}]


def row(n, y, h, x0=M, width=W - 2 * M):
    """Evenly spaced boxes across a row."""
    w = (width - GAP * (n - 1)) / n
    return [(round(x0 + i * (w + GAP)), y, round(w), h) for i in range(n)]


# ---- pages -------------------------------------------------------------------
def overview():
    p = Page("overview", "Overview")
    p.add("title", M, 16, 1100, 80, textbox(
        "Environmental Sensor Analytics",
        sub="Live DirectQuery over the Kubernetes pipeline · TimescaleDB read replica"))
    p.add("zone", 1480, 16, W - M - 1480, 80, slicer(col("bi dim_sensor", "sensor_group")), "Zone")
    cards = [
        measure("Sensors Online"),
        measure("Readings per Second", fmt="0.0"),
        measure("Avg AQI", fmt="0"),
        measure("AQI Category"),
        measure("Avg PM2.5", fmt="0.0"),
        measure("Alert Status Label"),
        measure("Latency p95 (s)", fmt="0.000"),
    ]
    for i, (box, proj) in enumerate(zip(row(len(cards), 112, 140), cards)):
        p.add(f"card{i}", *box, card(proj))

    p.add("trend", M, 268, 1180, 440, chart(
        "lineChart", col(FACT, "bucket"), [measure("Avg PM2.5", fmt="0.0")],
        series=col("bi dim_sensor", "sensor_group")), "PM2.5 by zone (1-minute average, µg/m³)")

    aqi = measure("Avg AQI", fmt="0")
    # Bar colour from the AQI Status Color measure ("fx > Field value").
    status_fill = {"dataPoint": [{
        "properties": {"fill": {"solid": {"color": {"expr": {"Measure": {
            "Expression": _src(FACT), "Property": "AQI Status Color"}}}}}},
        "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}]},
    }]}
    p.add("zones", 1220, 268, W - M - 1220, 440, chart(
        "clusteredBarChart", col("bi dim_sensor", "sensor_group"), [aqi],
        sort=sort_desc(aqi), objects=status_fill), "Average AQI by zone")

    bucket = col("bi alerts", "bucket")
    p.add("alerts", M, 724, W - 2 * M, H - 724 - M, table(
        [bucket, col("bi alerts", "severity"), col("bi alerts", "sensor_id"), col("bi alerts", "sensor_group"),
         col("bi alerts", "label"), agg("bi alerts", "value", "max", display="value", fmt="0.0"),
         col("bi alerts", "unit")],
        sort=sort_desc(bucket)), "Threshold breaches (last 24 h)")
    return p


def drilldown():
    p = Page("drilldown", "Sensor drill-down")
    p.add("title", M, 16, 1000, 80, textbox(
        "Sensor drill-down", sub="Pick a zone or a single sensor; every chart and the table follow"))
    p.add("zone", 1048, 16, 416, 80, slicer(col("bi dim_sensor", "sensor_group")), "Zone")
    p.add("sensor", 1480, 16, W - M - 1480, 80, slicer(col("bi dim_sensor", "sensor_id")), "Sensor")
    metrics = [("Avg Temperature °C", "Temperature (°C)", "0.0"), ("Avg Humidity %", "Humidity (%)", "0.0"),
               ("Avg PM2.5", "PM2.5 (µg/m³)", "0.0"), ("Avg CO2 ppm", "CO₂ (ppm)", "0")]
    boxes = row(2, 112, 300) + row(2, 428, 300)
    for i, ((m, label, fmt), box) in enumerate(zip(metrics, boxes)):
        p.add(f"m{i}", *box, chart("lineChart", col(FACT, "bucket"), [measure(m, fmt=fmt)]), label)
    t = "bi latest_by_sensor"
    aqi = col(t, "aqi")
    p.add("latest", M, 744, W - 2 * M, H - 744 - M, table(
        [col(t, "sensor_id"), col(t, "sensor_group"), col(t, "ts"), aqi, col(t, "aqi_category"),
         col(t, "pm25"), col(t, "pm10"), col(t, "temperature_c"), col(t, "humidity"), col(t, "co2"),
         col(t, "is_anomaly")],
        sort=sort_desc(aqi)), "Latest reading per sensor (worst AQI first)")
    return p


def anomalies():
    p = Page("anomalies", "Anomalies")
    p.add("title", M, 16, 1400, 80, textbox(
        "Anomalies and threshold breaches",
        sub="Anomalies: per-sensor EWMA z-score > 4 · Breaches: 1-minute averages over the thresholds table"))
    cards = [measure("Anomalous Readings"), measure("Anomaly Rate", fmt="0.00%"),
             measure("Sensors in Breach"), measure("Critical Breaches")]
    for i, (box, proj) in enumerate(zip(row(4, 112, 140), cards)):
        p.add(f"card{i}", *box, card(proj))
    t = "bi anomalies"
    count = agg(t, "ts", "count", display="anomalies")
    p.add("bymetric", *row(2, 268, 380)[0], chart(
        "clusteredColumnChart", col(t, "metric"), [count], series=col(t, "sensor_group"),
        sort=sort_desc(count)), "Anomalies by metric and zone (last 24 h)")
    count2 = agg(t, "ts", "count", display="anomalies")
    p.add("bysensor", *row(2, 268, 380)[1], chart(
        "clusteredBarChart", col(t, "sensor_id"), [count2], sort=sort_desc(count2)),
        "Anomalies per sensor (most first)")
    ts = col(t, "ts")
    p.add("detail", M, 664, W - 2 * M, H - 664 - M, table(
        [ts, col(t, "sensor_id"), col(t, "sensor_group"), col(t, "metric"),
         agg(t, "pm25", "max", display="pm25", fmt="0.0"), agg(t, "pm10", "max", display="pm10", fmt="0.0"),
         agg(t, "aqi", "max", display="aqi")],
        sort=sort_desc(ts)), "Anomalous readings")
    return p


def pipeline():
    p = Page("pipeline", "Pipeline")
    p.add("title", M, 16, 1400, 80, textbox(
        "Pipeline health",
        sub="Throughput and end-to-end latency (sensor timestamp → database commit)"))
    k = "bi kpi_now"
    cards = [agg(k, "readings_5m", "max", display="Readings (last 5 min)"),
             measure("Pipeline Throughput (rec/s)", fmt="0.0"),
             measure("Latency p50 (s)", fmt="0.000"),
             measure("Latency p95 (s)", fmt="0.000"),
             agg(k, "gap_filled_5m", "max", display="Gap-filled (last 5 min)"),
             agg(k, "anomalies_5m", "max", display="Anomalies (last 5 min)")]
    for i, (box, proj) in enumerate(zip(row(len(cards), 112, 140), cards)):
        p.add(f"card{i}", *box, card(proj))
    t = "bi pipeline_1m"
    left, right = row(2, 268, H - 268 - M)
    p.add("throughput", *left, chart(
        "lineChart", col(t, "minute"), [agg(t, "records_per_s", "avg", display="records per second", fmt="0.0")]),
        "Records committed per second")
    p.add("latency", *right, chart(
        "lineChart", col(t, "minute"),
        [agg(t, "latency_p50_s", "avg", display="p50", fmt="0.000"),
         agg(t, "latency_p95_s", "avg", display="p95", fmt="0.000"),
         agg(t, "latency_p99_s", "avg", display="p99", fmt="0.000")]),
        "End-to-end latency percentiles (seconds)")
    return p


def build_files() -> dict[str, bytes]:
    pages = [overview(), drilldown(), anomalies(), pipeline()]
    files = {}
    root = "Report/definition/pages"
    files[f"{root}/pages.json"] = {"$schema": PAGES_SCHEMA, "pageOrder": [p.name for p in pages],
                                   "activePageName": pages[0].name}
    for p in pages:
        files[f"{root}/{p.name}/page.json"] = {
            "$schema": PAGE_SCHEMA, "name": p.name, "displayName": p.display,
            "displayOption": "FitToPage", "height": H, "width": W}
        for v in p.visuals:
            files[f"{root}/{p.name}/visuals/{v['name']}/visual.json"] = v
    return {path: json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8") for path, obj in files.items()}


def main(src: str, dst: str) -> None:
    new_files = build_files()
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        if "Report/definition/report.json" not in names:
            sys.exit("This .pbix does not use the PBIR report format (no Report/definition/report.json).")
        for info in zin.infolist():
            name = info.filename
            if name.startswith("Report/definition/pages/") or name == "SecurityBindings":
                continue
            data = zin.read(name)
            if name == "[Content_Types].xml":
                text_ = data.decode("utf-8-sig")
                text_ = text_.replace('<Override PartName="/SecurityBindings" ContentType="" />', "")
                data = ("﻿" + text_).encode("utf-8")
            zout.writestr(info, data)
        for path, data in new_files.items():
            zout.writestr(path, data)
    print(f"wrote {dst}: {sum(1 for p in new_files if p.endswith('visual.json'))} visuals on "
          f"{sum(1 for p in new_files if p.endswith('page.json'))} pages")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
