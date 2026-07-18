"""DEV-ONLY synthetic history seed for developing the ML pipeline (Part 3).

Real training data doesn't exist yet: the CPCB API only returns current
snapshots (no historical backfill), and the scheduler has only run a handful
of times. Prophet needs weeks of hourly history per station. So we generate a
realistic synthetic series to build and validate the train -> forecast -> serve
pipeline NOW. When real data accumulates, the same pipeline retrains on it with
zero code change.

The signal is deliberately structured (diurnal + weekly + trend + noise +
spikes) so Prophet has real seasonality to fit. It is NOT real air quality.

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
POLLUTANT = "PM2.5"
DAYS = 430                       # ~14 months so history includes an Oct-Nov season
BASELINE = 90.0                 # ug/m3, roughly Delhi-ish
STUBBLE_LIFT = 60.0             # season-long elevation from farm fires
DIWALI_LIFT = 120.0             # firecracker spike
SEED = 42


def synth_value(ts: datetime, day_index: int, rng: random.Random) -> float:
    """Structured-but-noisy PM2.5 for one timestamp."""
    hour = ts.hour
    # Twice-daily diurnal cycle: morning + evening peaks, midday dip.
    diurnal = 18 * math.sin(2 * math.pi * hour / 24) + 10 * math.sin(4 * math.pi * hour / 24)
    # Weekends a bit cleaner (Sat=5, Sun=6).
    weekend = -8 if ts.weekday() >= 5 else 0
    # Gentle upward trend over the window.
    trend = 0.03 * day_index
    noise = rng.gauss(0, 8)
    # Rare random pollution spike.
    spike = rng.uniform(50, 150) if rng.random() < 0.01 else 0
    # Delhi seasonal drivers (the effects the regressors should learn).
    stubble = STUBBLE_LIFT if is_stubble_season(ts) else 0
    diwali = DIWALI_LIFT if in_diwali_window(ts) else 0
    return max(5.0, BASELINE + diurnal + weekend + trend + noise + spike + stubble + diwali)


def seed() -> None:
    rng = random.Random(SEED)
    start = datetime.now(timezone.utc) - timedelta(days=DAYS)
    start = start.replace(minute=0, second=0, microsecond=0)
    total_hours = DAYS * 24

    rows = []
    for station_id in STATION_IDS:
        for h in range(total_hours):
            ts = start + timedelta(hours=h)
            rows.append((station_id, POLLUTANT, round(synth_value(ts, h // 24, rng), 1), ts))

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO readings (station_id, pollutant, value, measured_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (station_id, pollutant, measured_at) DO NOTHING
                """,
                rows,
            )
        conn.commit()
    print(f"seeded ~{len(rows)} synthetic {POLLUTANT} rows across stations {STATION_IDS}")


def clear() -> None:
    """Remove seeded data. DEV ONLY: deletes ALL rows for these station/pollutant."""
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM readings WHERE pollutant = %s AND station_id = ANY(%s)",
                (POLLUTANT, STATION_IDS),
            )
            deleted = cur.rowcount
        conn.commit()
    print(f"cleared {deleted} rows for {POLLUTANT} on stations {STATION_IDS}")


def main() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")
    ap = argparse.ArgumentParser()
    ap.add_argument("--clear", action="store_true", help="delete seeded data instead of inserting")
    args = ap.parse_args()
    clear() if args.clear else seed()


if __name__ == "__main__":
    main()
