"""Validator, transformer and anomaly detector for one sensor record.

Everything here is pure: the process worker loads per-sensor state from
Redis, calls these functions, and writes the state back. That keeps the
worker pods stateless so the HPA can add or remove them freely.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import aqi as aqi_mod

METRICS = ("temperature_c", "humidity", "pressure_hpa", "pm25", "pm10", "co2")

# Physical sensor limits; anything outside is a faulty reading, not an anomaly.
RANGES = {
    "temperature_c": (-40.0, 85.0),
    "humidity": (0.0, 100.0),
    "pressure_hpa": (300.0, 1100.0),
    "pm25": (0.0, 1000.0),
    "pm10": (0.0, 2000.0),
    "co2": (250.0, 10000.0),
}


class ValidationError(ValueError):
    pass


@dataclass
class Reading:
    sensor_id: str
    group: str
    ts: datetime
    ingest_ts: datetime
    seq: int | None = None
    temperature_c: float | None = None
    humidity: float | None = None
    pressure_hpa: float | None = None
    pm25: float | None = None
    pm10: float | None = None
    co2: float | None = None
    aqi: int | None = None
    aqi_category: str | None = None
    gap_filled: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def _parse_ts(value) -> datetime:
    if value is None:
        raise ValidationError("missing ts")
    if isinstance(value, (int, float)):
        # Accept epoch seconds or epoch milliseconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"bad ts {value!r}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ValidationError(f"bad ts type {type(value).__name__}")


def _num(raw: dict, key: str) -> float | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} is not numeric: {value!r}") from exc
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _temperature_c(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    unit = (unit or "C").upper()
    if unit in ("C", "CELSIUS"):
        return value
    if unit in ("F", "FAHRENHEIT"):
        return (value - 32.0) * 5.0 / 9.0
    if unit in ("K", "KELVIN"):
        return value - 273.15
    raise ValidationError(f"unknown temperature unit {unit!r}")


def _pressure_hpa(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    unit = (unit or "hPa").lower()
    factors = {"hpa": 1.0, "mbar": 1.0, "kpa": 10.0, "pa": 0.01, "inhg": 33.8639, "mmhg": 1.33322}
    if unit not in factors:
        raise ValidationError(f"unknown pressure unit {unit!r}")
    return value * factors[unit]


def validate_and_normalise(raw: dict) -> Reading:
    """Schema and range checks plus unit normalisation.

    Raises ValidationError for records that belong in the dead-letter queue.
    """
    if not isinstance(raw, dict):
        raise ValidationError("payload is not an object")
    sensor_id = raw.get("sensor_id")
    if not sensor_id or not isinstance(sensor_id, str):
        raise ValidationError("missing sensor_id")

    reading = Reading(
        sensor_id=sensor_id,
        group=str(raw.get("group") or "default"),
        ts=_parse_ts(raw.get("ts")),
        ingest_ts=_parse_ts(raw.get("ingest_ts")) if raw.get("ingest_ts") else datetime.now(timezone.utc),
        seq=int(raw["seq"]) if isinstance(raw.get("seq"), (int, float)) else None,
        temperature_c=_temperature_c(_num(raw, "temperature"), raw.get("temperature_unit", "C")),
        humidity=_num(raw, "humidity"),
        pressure_hpa=_pressure_hpa(_num(raw, "pressure"), raw.get("pressure_unit", "hPa")),
        pm25=_num(raw, "pm25"),
        pm10=_num(raw, "pm10"),
        co2=_num(raw, "co2"),
    )

    if all(getattr(reading, m) is None for m in METRICS):
        raise ValidationError("no metric values")

    for metric, (lo, hi) in RANGES.items():
        value = getattr(reading, metric)
        if value is not None and not lo <= value <= hi:
            raise ValidationError(f"{metric}={value:.2f} outside [{lo}, {hi}]")

    # A clock far in the future means the node's RTC is wrong.
    if (reading.ts - reading.ingest_ts).total_seconds() > 300:
        raise ValidationError("ts more than 5 minutes in the future")

    return reading


def gap_fill(reading: Reading, last: dict, max_age_s: float) -> None:
    """Forward-fill missing metrics from the sensor's last good reading."""
    last_ts = last.get("ts")
    if last_ts is None or (reading.ts.timestamp() - float(last_ts)) > max_age_s:
        return
    for metric in METRICS:
        if getattr(reading, metric) is None and last.get(metric) is not None:
            setattr(reading, metric, float(last[metric]))
            reading.gap_filled.append(metric)


def derive(reading: Reading) -> None:
    reading.aqi = aqi_mod.aqi(reading.pm25, reading.pm10)
    reading.aqi_category = aqi_mod.category(reading.aqi)


def detect_anomalies(reading: Reading, state: dict, z: float, alpha: float, warmup: int) -> dict:
    """EWMA z-score per metric. Returns the updated state.

    The state holds a running mean and variance for each metric. Values that
    were gap-filled are not scored, and anomalous values are not folded into
    the baseline so one spike cannot drag it.
    """
    new_state = dict(state)
    count = int(state.get("n", 0))
    for metric in METRICS:
        value = getattr(reading, metric)
        if value is None or metric in reading.gap_filled:
            continue
        mean = state.get(f"{metric}_mean")
        var = state.get(f"{metric}_var")
        if mean is None:
            new_state[f"{metric}_mean"] = value
            new_state[f"{metric}_var"] = 0.0
            continue
        mean, var = float(mean), float(var)
        std = math.sqrt(var)
        is_anomaly = count >= warmup and std > 1e-9 and abs(value - mean) / std > z
        if is_anomaly:
            reading.anomalies.append(metric)
            continue
        diff = value - mean
        mean += alpha * diff
        var = (1 - alpha) * (var + alpha * diff * diff)
        new_state[f"{metric}_mean"] = mean
        new_state[f"{metric}_var"] = var
    new_state["n"] = count + 1
    return new_state


def last_values(reading: Reading) -> dict:
    out = {"ts": reading.ts.timestamp()}
    for metric in METRICS:
        value = getattr(reading, metric)
        if value is not None and metric not in reading.anomalies:
            out[metric] = value
    return out
