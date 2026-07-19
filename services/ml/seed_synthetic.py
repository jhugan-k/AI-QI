"""DEV-ONLY synthetic history seed for developing the ML pipeline (Part 3).

Real training data doesn't exist yet: the CPCB API only returns current
snapshots (no historical backfill), and the scheduler has only run a handful
of times. Prophet needs weeks of hourly history per station. So we generate a
realistic synthetic series to build and validate the train -> forecast -> serve
pipeline NOW. When real data accumulates, the same pipeline retrains on it with
zero code change.

The signal is deliberately structured so Prophet has real seasonality to fit:
  - diurnal + weekly + trend + noise + rare spikes
  - Delhi seasonal drivers (stubble burning, Diwali) — the event regressors
  - WEATHER coupling: wind disperses particulates, so PM falls as wind rises.
    The weather series is seeded too, and drives PM, so the weather regressors
    (Part 3, wired in Part 5+) have genuine signal to learn.
It is NOT real air quality.

Seed:   python -m services.ml.seed_synthetic
Clear:  python -m services.ml.seed_synthetic --clear
"""

from __future__ import annotations

import argparse
import math
import os
import random
from datetime import datetime, timedelta, timezone

import psycopg
from dotenv import load_dotenv

from services.ml.regressors import in_diwali_window, is_stubble_season

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

# Stations to seed (must already exist in the stations table) and horizon.
STATION_IDS = [30, 2, 5]        # Anand Vihar, ITO, IIT Delhi
POLLUTANTS = ["PM2.5", "PM10"]  # PM10 is seeded as coarser, correlated with PM2.5
DAYS = 430                       # ~14 months so history includes an Oct-Nov season
BASELINE = 90.0                 # ug/m3, roughly Delhi-ish
STUBBLE_LIFT = 60.0             # season-long elevation from farm fires
DIWALI_LIFT = 120.0             # firecracker spike
WIND_DISPERSAL = 12.0          # ug/m3 removed per m/s of wind above the mean
MEAN_WIND = 2.5                # m/s, the wind the baseline is calibrated at
SEED = 42


def synth_weather(ts: datetime, rng: random.Random) -> dict:
    """Structured hourly weather. Wind is the field that matters for dispersal."""
    hour = ts.hour
    # Wind peaks mid-afternoon (surface heating), calm at night.
    wind = MEAN_WIND + 1.5 * math.sin(2 * math.pi * (hour - 14) / 24) + rng.gauss(0, 0.8)
    wind = max(0.2, round(wind, 1))
    # Temperature: diurnal + coarse seasonal (warmer May-ish, cooler Dec-ish).
    seasonal = 8 * math.sin(2 * math.pi * (ts.timetuple().tm_yday - 100) / 365)
    temp = 25 + seasonal + 7 * math.sin(2 * math.pi * (hour - 15) / 24) + rng.gauss(0, 1.5)
    # Humidity moves inversely to temperature.
    humidity = int(min(95, max(15, 70 - 1.2 * (temp - 25) + rng.gauss(0, 5))))
    return {
        "temp_c": round(temp, 1),
        "humidity": humidity,
        "pressure": int(1010 + rng.gauss(0, 4)),
        "wind_speed": wind,
        "wind_deg": rng.randint(0, 359),
        "clouds": rng.randint(0, 100),
    }


def synth_pm25(ts: datetime, day_index: int, wind: float, rng: random.Random) -> float:
    """Structured-but-noisy PM2.5 for one timestamp, responsive to wind."""
    hour = ts.hour
    # Twice-daily diurnal cycle: morning + evening peaks, midday dip.
    diurnal = 18 * math.sin(2 * math.pi * hour / 24) + 10 * math.sin(4 * math.pi * hour / 24)
    weekend = -8 if ts.weekday() >= 5 else 0          # weekends a bit cleaner
    trend = 0.03 * day_index                          # gentle upward trend
    noise = rng.gauss(0, 8)
    spike = rng.uniform(50, 150) if rng.random() < 0.01 else 0
    stubble = STUBBLE_LIFT if is_stubble_season(ts) else 0
    diwali = DIWALI_LIFT if in_diwali_window(ts) else 0
    dispersal = -WIND_DISPERSAL * (wind - MEAN_WIND)  # wind clears the air
    return max(
        5.0,
        BASELINE + diurnal + weekend + trend + noise + spike + stubble + diwali + dispersal,
    )


def synth_pm10(pm25: float, rng: random.Random) -> float:
    """PM10 as a coarser fraction: strongly correlated with, and above, PM2.5."""
    return round(max(pm25, pm25 * 1.8 + 20 + rng.gauss(0, 15)), 1)


def seed() -> None:
    rng = random.Random(SEED)
    start = datetime.now(timezone.utc) - timedelta(days=DAYS)
    start = start.replace(minute=0, second=0, microsecond=0)
    total_hours = DAYS * 24

    reading_rows: list[tuple] = []
    weather_rows: list[tuple] = []
    for station_id in STATION_IDS:
        for h in range(total_hours):
            ts = start + timedelta(hours=h)
            wx = synth_weather(ts, rng)
            weather_rows.append((
                station_id, ts, wx["temp_c"], wx["humidity"], wx["pressure"],
                wx["wind_speed"], wx["wind_deg"], wx["clouds"],
            ))
            pm25 = round(synth_pm25(ts, h // 24, wx["wind_speed"], rng), 1)
            reading_rows.append((station_id, "PM2.5", pm25, ts))
            reading_rows.append((station_id, "PM10", synth_pm10(pm25, rng), ts))

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO readings (station_id, pollutant, value, measured_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (station_id, pollutant, measured_at) DO NOTHING
                """,
                reading_rows,
            )
            cur.executemany(
                """
                INSERT INTO weather
                    (station_id, measured_at, temp_c, humidity, pressure,
                     wind_speed, wind_deg, clouds)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (station_id, measured_at) DO NOTHING
                """,
                weather_rows,
            )
        conn.commit()
    print(f"seeded ~{len(reading_rows)} reading rows ({', '.join(POLLUTANTS)}) "
          f"and {len(weather_rows)} weather rows across stations {STATION_IDS}")


def clear() -> None:
    """Remove seeded data. DEV ONLY: deletes ALL rows for these stations."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM readings WHERE pollutant = ANY(%s) AND station_id = ANY(%s)",
                (POLLUTANTS, STATION_IDS),
            )
            r_deleted = cur.rowcount
            cur.execute("DELETE FROM weather WHERE station_id = ANY(%s)", (STATION_IDS,))
            w_deleted = cur.rowcount
        conn.commit()
    print(f"cleared {r_deleted} reading rows and {w_deleted} weather rows on stations {STATION_IDS}")


def main() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true", help="delete seeded data instead of inserting")
    args = ap.parse_args()
    clear() if args.clear else seed()


if __name__ == "__main__":
    main()
