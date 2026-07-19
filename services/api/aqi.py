"""CPCB National AQI sub-index computation (backend).

Mirrors the breakpoint table used client-side in web/app.js. Kept here so the
API can compute a station's *overall* AQI = the worst pollutant sub-index (the
CPCB definition), which powers the /ranking "worst areas" endpoint.

Each breakpoint row is [concLow, concHigh, aqiLow, aqiHigh]: within a band the
sub-index interpolates linearly between aqiLow and aqiHigh.
"""

from __future__ import annotations

CATEGORIES = ["Good", "Satisfactory", "Moderate", "Poor", "Very Poor", "Severe"]

BREAKPOINTS: dict[str, list[list[float]]] = {
    "PM2.5": [[0, 30, 0, 50], [31, 60, 51, 100], [61, 90, 101, 200], [91, 120, 201, 300], [121, 250, 301, 400], [251, 500, 401, 500]],
    "PM10":  [[0, 50, 0, 50], [51, 100, 51, 100], [101, 250, 101, 200], [251, 350, 201, 300], [351, 430, 301, 400], [431, 600, 401, 500]],
    "NO2":   [[0, 40, 0, 50], [41, 80, 51, 100], [81, 180, 101, 200], [181, 280, 201, 300], [281, 400, 301, 400], [401, 500, 401, 500]],
    "SO2":   [[0, 40, 0, 50], [41, 80, 51, 100], [81, 380, 101, 200], [381, 800, 201, 300], [801, 1600, 301, 400], [1601, 2000, 401, 500]],
    "CO":    [[0, 1, 0, 50], [1.1, 2, 51, 100], [2.1, 10, 101, 200], [10.1, 17, 201, 300], [17.1, 34, 301, 400], [34.1, 50, 401, 500]],
    "OZONE": [[0, 50, 0, 50], [51, 100, 51, 100], [101, 168, 101, 200], [169, 208, 201, 300], [209, 748, 301, 400], [749, 1000, 401, 500]],
    "NH3":   [[0, 200, 0, 50], [201, 400, 51, 100], [401, 800, 101, 200], [801, 1200, 201, 300], [1201, 1800, 301, 400], [1801, 2000, 401, 500]],
}


def sub_index(pollutant: str, conc: float | None) -> tuple[int, str] | None:
    """(sub_index, category) for one pollutant concentration, or None if unmapped."""
    if conc is None:
        return None
    bands = BREAKPOINTS.get(pollutant)
    if not bands:
        return None
    for i, (c_lo, c_hi, a_lo, a_hi) in enumerate(bands):
        if conc <= c_hi:
            c = max(conc, c_lo)
            idx = round((a_hi - a_lo) / (c_hi - c_lo) * (c - c_lo) + a_lo)
            return idx, CATEGORIES[i]
    return 500, "Severe"   # above the top band → capped at Severe


# Pollutants excluded from the overall-AQI roll-up. The CPCB CO sub-index is
# defined in mg/m3, but the data.gov.in real-time feed publishes CO values in the
# tens-to-hundreds (24-162 in our live snapshot) — physically impossible as mg/m3
# ambient air, so the unit is mismatched/ambiguous. Left in, CO saturates every
# station to Severe and swamps the ranking. Excluded here until the feed's CO unit
# is confirmed; PM2.5/PM10 (the real Delhi AQI drivers) still set the headline.
OVERALL_EXCLUDE = {"CO"}


def overall_aqi(readings: dict[str, float | None]) -> tuple[int, str, str] | None:
    """Overall AQI for a station from its latest per-pollutant readings.

    CPCB defines a station's AQI as the MAX sub-index across pollutants (the
    single worst pollutant drives the headline number). Returns
    (aqi, category, dominant_pollutant), or None if nothing is mappable.
    Pollutants in OVERALL_EXCLUDE are skipped (see the note there).
    """
    best: tuple[int, str, str] | None = None
    for pollutant, conc in readings.items():
        if pollutant in OVERALL_EXCLUDE:
            continue
        si = sub_index(pollutant, conc)
        if si is None:
            continue
        idx, category = si
        if best is None or idx > best[0]:
            best = (idx, category, pollutant)
    return best
