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

from services.ml.train import MIN_TRAIN_POINTS, artifact_path, forecast, train

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")

FORECAST_HOURS = 24


def trainable_series(conn) -> list[tuple[int, str]]:
    """Every (station_id, pollutant) with enough history to fit a model.

    Data-driven so the batch covers whatever has accumulated — no hardcoded
    station/pollutant list to keep in sync as coverage grows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT station_id, pollutant
            FROM readings
            WHERE value IS NOT NULL
            GROUP BY station_id, pollutant
            HAVING count(*) >= %s
            ORDER BY station_id, pollutant
            """,
            (MIN_TRAIN_POINTS,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


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
        series = trainable_series(conn)
        print(f"batch: {len(series)} trainable (station, pollutant) series")
        for station_id, pollutant in series:
            try:
                model, n = train(station_id, pollutant)
                fc = forecast(model, hours, station_id)
                version = os.path.basename(artifact_path(station_id, pollutant))
                stored = store_forecast(conn, station_id, pollutant, fc, version)
                conn.commit()
                print(f"station {station_id}/{pollutant}: trained on {n} pts, stored {stored} rows")
            except Exception as exc:  # noqa: BLE001 — one bad series shouldn't stop the batch
                conn.rollback()
                print(f"station {station_id}/{pollutant}: FAILED — {exc}")


if __name__ == "__main__":
    run_batch()
