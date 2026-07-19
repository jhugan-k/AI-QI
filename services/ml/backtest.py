"""Holdout backtest: score the model against data it never saw (Part 5).

The daily batch (batch.py) forecasts the *future*, so its predictions can't be
scored until real readings catch up. To evaluate model quality now, we backtest:
hold out the most recent `HOLDOUT_HOURS`, fit a model on everything *before* that
cutoff, then predict the held-out window — for which we already have actuals.

The predictions are written to the forecasts table under a distinct
model_version ('backtest') and with target_times in the past, so the API's
/accuracy endpoint can join them against the real readings and score them.

Because it holds the window out of training, this is an honest out-of-sample
test (not an optimistic in-sample fit). The production pickle is left untouched.

Run:  python -m services.ml.backtest
"""

from __future__ import annotations

import os

import pandas as pd
import psycopg
from dotenv import load_dotenv

from services.ml.batch import store_forecast, trainable_series
from services.ml.train import MIN_TRAIN_POINTS, build_model, load_history

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
HOLDOUT_HOURS = 48
BACKTEST_VERSION = "backtest"


def backtest_station(conn, station_id: int, pollutant: str) -> int:
    """Fit on history up to a cutoff, predict the held-out tail, store the preds.

    Returns the number of prediction rows written (0 if skipped).
    """
    df = load_history(station_id, pollutant)          # ds, y, event + weather regressors
    if df.empty:
        print(f"station {station_id}/{pollutant}: no history — skipped")
        return 0

    cutoff = df["ds"].max() - pd.Timedelta(hours=HOLDOUT_HOURS)
    train_df = df[df["ds"] <= cutoff]
    holdout = df[df["ds"] > cutoff]

    if len(train_df) < MIN_TRAIN_POINTS:
        print(f"station {station_id}/{pollutant}: only {len(train_df)} pre-cutoff points "
              f"(need {MIN_TRAIN_POINTS}) — skipped")
        return 0
    if holdout.empty:
        print(f"station {station_id}/{pollutant}: empty holdout window — skipped")
        return 0

    # Identical model path to production training, fit on the truncated set only.
    model = build_model(train_df)

    # The holdout rows already carry every regressor column (from load_history),
    # incl. the ACTUAL weather for those hours — an honest test. Drop y for predict.
    probe = holdout.drop(columns=["y"])
    pred = model.predict(probe)[["ds", "yhat", "yhat_lower", "yhat_upper"]]

    stored = store_forecast(conn, station_id, pollutant, pred, BACKTEST_VERSION)
    conn.commit()
    print(f"station {station_id}/{pollutant}: trained on {len(train_df)} pts, "
          f"backtested {stored} points over the last {HOLDOUT_HOURS}h")
    return stored


def run() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")
    with psycopg.connect(DATABASE_URL) as conn:
        series = trainable_series(conn)
        print(f"backtest: {len(series)} trainable (station, pollutant) series")
        for station_id, pollutant in series:
            try:
                backtest_station(conn, station_id, pollutant)
            except Exception as exc:  # noqa: BLE001 — one bad series shouldn't stop the run
                conn.rollback()
                print(f"station {station_id}/{pollutant}: FAILED — {exc}")


if __name__ == "__main__":
    run()
