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
from prophet import Prophet

from services.ml.batch import STATIONS, POLLUTANT, store_forecast
from services.ml.regressors import add_event_regressors
from services.ml.train import MIN_TRAIN_POINTS, load_history

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
HOLDOUT_HOURS = 48
BACKTEST_VERSION = "backtest"


def backtest_station(conn, station_id: int, pollutant: str) -> int:
    """Fit on history up to a cutoff, predict the held-out tail, store the preds.

    Returns the number of prediction rows written (0 if skipped).
    """
    df = load_history(station_id, pollutant)          # ds (IST-naive), y, regressors
    if df.empty:
        print(f"station {station_id}: no history — skipped")
        return 0

    cutoff = df["ds"].max() - pd.Timedelta(hours=HOLDOUT_HOURS)
    train_df = df[df["ds"] <= cutoff]
    holdout = df[df["ds"] > cutoff]

    if len(train_df) < MIN_TRAIN_POINTS:
        print(f"station {station_id}: only {len(train_df)} pre-cutoff points "
              f"(need {MIN_TRAIN_POINTS}) — skipped")
        return 0
    if holdout.empty:
        print(f"station {station_id}: empty holdout window — skipped")
        return 0

    # Same model config as production training (train.py), fit on the truncated set.
    model = Prophet(daily_seasonality=True, weekly_seasonality=True, yearly_seasonality=False)
    model.add_regressor("stubble")
    model.add_regressor("diwali")
    model.fit(train_df)

    # Predict exactly the held-out timestamps so the scoring join lines up.
    probe = add_event_regressors(holdout[["ds"]].copy())
    pred = model.predict(probe)[["ds", "yhat", "yhat_lower", "yhat_upper"]]

    stored = store_forecast(conn, station_id, pollutant, pred, BACKTEST_VERSION)
    conn.commit()
    print(f"station {station_id}: trained on {len(train_df)} pts, "
          f"backtested {stored} points over the last {HOLDOUT_HOURS}h")
    return stored


def run() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")
    with psycopg.connect(DATABASE_URL) as conn:
        for station_id in STATIONS:
            try:
                backtest_station(conn, station_id, POLLUTANT)
            except Exception as exc:  # noqa: BLE001 — one bad station shouldn't stop the run
                conn.rollback()
                print(f"station {station_id}: FAILED — {exc}")


if __name__ == "__main__":
    run()
