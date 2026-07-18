"""Batch: train every station's model and persist its 24h forecast (Part 3).

For each configured (station, pollutant): train a model, save its artifact,
forecast the next 24h, and write those predictions to the forecasts table.
This is the job a daily cron runs. The API then serves forecasts straight from
the table (fast, and available even if the ML service is down).

Run:  python -m services.ml.batch
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

from services.ml.train import artifact_path, forecast, train

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

# What to model. Currently the synthetically-seeded stations / pollutant.
STATIONS = [30, 2, 5]
POLLUTANT = "PM2.5"
FORECAST_HOURS = 24


def store_forecast(conn, station_id, pollutant, fc_df, model_version) -> int:
    """Persist a forecast frame. ds is IST-naive -> convert back to UTC tstz."""
    rows = [
        (
            station_id,
            pollutant,
            r["ds"].tz_localize("Asia/Kolkata").tz_convert("UTC").to_pydatetime(),
            float(r["yhat"]),
            float(r["yhat_lower"]),
            float(r["yhat_upper"]),
            model_version,
        )
        for _, r in fc_df.iterrows()
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO forecasts
                (station_id, pollutant, target_time, yhat, yhat_lower, yhat_upper, model_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT forecasts_unique DO NOTHING
            """,
            rows,
        )
    return len(rows)


def run_batch(hours: int = FORECAST_HOURS) -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")
    with psycopg.connect(DATABASE_URL) as conn:
        for station_id in STATIONS:
            try:
                model, n = train(station_id, POLLUTANT)
                fc = forecast(model, hours)
                version = os.path.basename(artifact_path(station_id, POLLUTANT))
                stored = store_forecast(conn, station_id, POLLUTANT, fc, version)
                conn.commit()
                print(f"station {station_id}: trained on {n} pts, stored {stored} forecast rows")
            except Exception as exc:  # noqa: BLE001 — one bad station shouldn't stop the batch
                conn.rollback()
                print(f"station {station_id}: FAILED — {exc}")


if __name__ == "__main__":
    run_batch()
