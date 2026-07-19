"""Weather as Prophet regressors (Part 5+).

Weather drives pollution dispersal — wind clears particulates, temperature and
humidity shape how they accumulate — so the meteorology ingested into the
`weather` table (Part 1) is wired in here as external regressors.

The awkward part of any weather regressor is the *future*: to forecast the next
24h we need weather for hours that haven't happened. We fill those from the
station's hour-of-day weather climatology (its typical value for that hour),
mirroring the hour-of-day imputation already used for pollutant gaps. Historical
rows use the actual reading where present and the same climatology to fill holes,
so the regressor columns are always fully populated for both fit and predict.
"""

from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
IST = ZoneInfo("Asia/Kolkata")

# The weather fields used as regressors. Kept small: a few well-supported
# drivers beat many collinear ones (temp/humidity/pressure move together).
WEATHER_REGRESSORS = ["wind_speed", "temp_c", "humidity"]
MIN_WEATHER_ROWS = 100     # below this there isn't enough to be worth modelling


def weather_history(station_id: int, conn: psycopg.Connection | None = None) -> pd.DataFrame:
    """Station weather as a frame keyed by IST-naive `ds` (to line up with load_history)."""
    own = conn is None
    conn = conn or psycopg.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT measured_at, wind_speed, temp_c, humidity "
                "FROM weather WHERE station_id = %s ORDER BY measured_at",
                (station_id,),
            )
            rows = cur.fetchall()
    finally:
        if own:
            conn.close()

    data = [
        (ts.astimezone(IST).replace(tzinfo=None), ws, tc, hu)
        for ts, ws, tc, hu in rows
    ]
    return pd.DataFrame(data, columns=["ds", "wind_speed", "temp_c", "humidity"])


def hourly_climatology(weather_df: pd.DataFrame) -> dict[int, dict[str, float]]:
    """Mean of each weather regressor per hour-of-day (0-23) — the future filler."""
    if weather_df.empty:
        return {}
    hours = weather_df["ds"].dt.hour
    clim: dict[int, dict[str, float]] = {}
    for hour, grp in weather_df.groupby(hours):
        clim[int(hour)] = {col: float(grp[col].mean()) for col in WEATHER_REGRESSORS}
    return clim


def has_weather_signal(weather_df: pd.DataFrame) -> bool:
    """True only if there's enough weather AND each regressor actually varies.

    A constant regressor has zero variance; Prophet standardises regressors and
    would divide by zero. Guarding here lets a station with no/flat weather fall
    back cleanly to the event-only model.
    """
    if len(weather_df) < MIN_WEATHER_ROWS:
        return False
    return all(weather_df[col].nunique() > 1 for col in WEATHER_REGRESSORS)


def attach_weather(
    df: pd.DataFrame,
    weather_df: pd.DataFrame,
    climatology: dict[int, dict[str, float]],
) -> pd.DataFrame:
    """Return `df` with every WEATHER_REGRESSOR column present and fully filled.

    Priority per row: actual weather at that `ds` -> hour-of-day climatology ->
    the column's overall mean -> 0.0. Works for both the training frame (has some
    actual weather) and a future frame (has none, so it's all climatology).
    """
    df = df.copy()
    if weather_df is not None and not weather_df.empty:
        df = df.merge(weather_df, on="ds", how="left")

    overall = {
        col: (float(weather_df[col].mean())
              if weather_df is not None and not weather_df.empty and col in weather_df
              else 0.0)
        for col in WEATHER_REGRESSORS
    }
    hours = df["ds"].dt.hour
    for col in WEATHER_REGRESSORS:
        if col not in df:
            df[col] = pd.NA
        clim_fill = hours.map(lambda h: climatology.get(int(h), {}).get(col))
        df[col] = df[col].fillna(clim_fill).fillna(overall[col]).fillna(0.0)
    return df
