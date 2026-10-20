"""US EPA Air Quality Index from particulate concentrations.

Breakpoints follow the EPA's 2024 revision of the PM2.5 NAAQS table. The
index for a reading is the higher of the PM2.5 and PM10 sub-indices.
"""
from __future__ import annotations

# (conc_low, conc_high, aqi_low, aqi_high)
PM25_BREAKPOINTS = [
    (0.0, 9.0, 0, 50),
    (9.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
]

PM10_BREAKPOINTS = [
    (0, 54, 0, 50),
    (55, 154, 51, 100),
    (155, 254, 101, 150),
    (255, 354, 151, 200),
    (355, 424, 201, 300),
    (425, 604, 301, 500),
]

CATEGORIES = [
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive Groups"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
    (500, "Hazardous"),
]


def _sub_index(conc: float, table: list[tuple], decimals: int) -> int:
    # EPA truncates the concentration before the lookup.
    factor = 10 ** decimals
    c = int(conc * factor) / factor
    for c_lo, c_hi, i_lo, i_hi in table:
        if c <= c_hi:
            c = max(c, c_lo)
            return round((i_hi - i_lo) / (c_hi - c_lo) * (c - c_lo) + i_lo)
    return 500


def aqi(pm25: float | None, pm10: float | None) -> int | None:
    parts = []
    if pm25 is not None:
        parts.append(_sub_index(pm25, PM25_BREAKPOINTS, 1))
    if pm10 is not None:
        parts.append(_sub_index(pm10, PM10_BREAKPOINTS, 0))
    return max(parts) if parts else None


def category(index: int | None) -> str | None:
    if index is None:
        return None
    for upper, name in CATEGORIES:
        if index <= upper:
            return name
    return "Hazardous"
