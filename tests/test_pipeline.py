import time

import pytest

from envbi_common import aqi
from envbi_common.pipeline import (
    ValidationError, derive, detect_anomalies, gap_fill, last_values, validate_and_normalise,
)


def raw(**kw):
    base = {"sensor_id": "n1", "group": "zone-a", "ts": int(time.time() * 1000),
            "temperature": 25.0, "humidity": 50, "pressure": 1012, "pm25": 12.0, "pm10": 20, "co2": 450}
    base.update(kw)
    return base


def test_unit_normalisation():
    r = validate_and_normalise(raw(temperature=77.0, temperature_unit="F", pressure=101.2, pressure_unit="kPa"))
    assert r.temperature_c == pytest.approx(25.0)
    assert r.pressure_hpa == pytest.approx(1012.0)


@pytest.mark.parametrize("bad, reason", [
    ({"sensor_id": None}, "sensor_id"),
    ({"humidity": 180}, "humidity"),
    ({"ts": "yesterday"}, "bad ts"),
    ({"temperature": "hot"}, "not numeric"),
    ({"temperature_unit": "R"}, "unknown temperature unit"),
])
def test_rejects(bad, reason):
    with pytest.raises(ValidationError, match=reason):
        validate_and_normalise(raw(**bad))


def test_no_metrics_rejected():
    with pytest.raises(ValidationError):
        validate_and_normalise({"sensor_id": "n1", "ts": int(time.time() * 1000)})


def test_gap_fill_uses_recent_values_only():
    r = validate_and_normalise(raw(humidity=None, co2=None))
    gap_fill(r, {"ts": r.ts.timestamp() - 10, "humidity": "55.5", "co2": "500"}, max_age_s=300)
    assert r.humidity == 55.5 and r.co2 == 500 and set(r.gap_filled) == {"humidity", "co2"}

    r2 = validate_and_normalise(raw(humidity=None))
    gap_fill(r2, {"ts": r2.ts.timestamp() - 600, "humidity": "55.5"}, max_age_s=300)
    assert r2.humidity is None and not r2.gap_filled


@pytest.mark.parametrize("pm25, expected", [(0, 0), (9.0, 50), (35.4, 100), (55.4, 150), (125.4, 200), (500, 500)])
def test_aqi_breakpoints(pm25, expected):
    assert aqi.aqi(pm25, None) == expected


def test_aqi_takes_worst_pollutant():
    assert aqi.aqi(5.0, 200) == aqi.aqi(None, 200) > aqi.aqi(5.0, None)
    assert aqi.category(42) == "Good" and aqi.category(160) == "Unhealthy"


def test_anomaly_flags_spike_and_keeps_baseline():
    state = {}
    for i in range(100):
        r = validate_and_normalise(raw(pm25=12.0 + (i % 5) * 0.2))
        state = detect_anomalies(r, state, z=4, alpha=0.05, warmup=30)
        assert not r.anomalies
    baseline = state["pm25_mean"]
    spike = validate_and_normalise(raw(pm25=120.0))
    state = detect_anomalies(spike, state, z=4, alpha=0.05, warmup=30)
    assert "pm25" in spike.anomalies
    assert state["pm25_mean"] == baseline  # spike not folded into the baseline
    assert "pm25" not in last_values(spike)


def test_derive_sets_category():
    r = validate_and_normalise(raw(pm25=40.0, pm10=50))
    derive(r)
    assert r.aqi == aqi.aqi(40.0, 50) and r.aqi_category == "Unhealthy for Sensitive Groups"
