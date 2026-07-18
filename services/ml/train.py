"""Per-station Prophet training + forecasting (Part 3).

Reads a station's pollutant history from Postgres, cleans it (reusing the
Part 2 pipeline), fits a Prophet model with daily + weekly seasonality, and
saves the model as a pickled artifact. One model per (station, pollutant).

Train station 30:  python -m services.ml.train
"""

from __future__ import annotations

import logging
import os
import pickle
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from dotenv import load_dotenv
from prophet import Prophet

from services.api.cleaning import validate
from services.ml.regressors import add_event_regressors

load_dotenv()
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)  # quiet Stan output

DATABASE_URL = os.environ.get("DATABASE_URL")
IST = ZoneInfo("Asia/Kolkata")
MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")
MIN_TRAIN_POINTS = 100   # Prophet needs a reasonable history to be meaningful


def artifact_path(station_id: int, pollutant: str) -> str:
    """e.g. .../models/model_station_30_PM25.pkl (pollutant sanitized for filename)."""
    safe = pollutant.replace(".", "").replace("/", "")
    return os.path.join(MODEL_DIR, f"model_station_{station_id}_{safe}.pkl")


def load_history(station_id: int, pollutant: str) -> pd.DataFrame:
    """History as a Prophet frame (ds, y). ds is IST-naive so daily seasonality
    lines up with local hours; Prophet dislikes tz-aware datetimes.

    IMPORTANT: for TRAINING we only `validate` (drop physically-impossible
    values) — we deliberately DO NOT run the statistical outlier removal used
    for serving. That de-outlier step would strip legitimate Diwali/stubble
    spikes (they look like outliers), which are exactly the signal the model
    must learn. Cleaning-for-display != cleaning-for-training.
    """
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT measured_at, value FROM readings "
                "WHERE station_id = %s AND pollutant = %s ORDER BY measured_at",
                (station_id, pollutant),
            )
            rows = cur.fetchall()

    timestamps = [r[0] for r in rows]
    validated = validate(pollutant, [r[1] for r in rows])
    data = [
        (ts.astimezone(IST).replace(tzinfo=None), v)
        for ts, v in zip(timestamps, validated)
        if v is not None
    ]
    df = pd.DataFrame(data, columns=["ds", "y"])
    return add_event_regressors(df)   # attach stubble + diwali columns


def train(station_id: int, pollutant: str = "PM2.5") -> tuple[Prophet, int]:
    """Fit and persist a model. Returns (model, n_training_points)."""
    df = load_history(station_id, pollutant)
    if len(df) < MIN_TRAIN_POINTS:
        raise ValueError(
            f"only {len(df)} usable points for station {station_id}/{pollutant}; "
            f"need >= {MIN_TRAIN_POINTS}"
        )

    model = Prophet(
        daily_seasonality=True,
        weekly_seasonality=True,
        yearly_seasonality=False,     # let the explicit regressors own Oct-Nov, not a yearly curve
    )
    model.add_regressor("stubble")    # farm-fire season indicator
    model.add_regressor("diwali")     # Diwali spike + lingering window (see regressors.py)
    model.fit(df)

    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(artifact_path(station_id, pollutant), "wb") as f:
        pickle.dump(model, f)
    return model, len(df)


def forecast(model: Prophet, hours: int = 24) -> pd.DataFrame:
    """Next `hours` of predictions with uncertainty bounds."""
    future = model.make_future_dataframe(periods=hours, freq="h")
    future = add_event_regressors(future)   # regressor must be present for the future too
    fc = model.predict(future)
    return fc[["ds", "yhat", "yhat_lower", "yhat_upper"]].tail(hours)


def _seasonal_demo(model: Prophet) -> None:
    """Show the model separates normal vs stubble vs Diwali contributions."""
    cases = {
        "normal   (2026-08-15 10:00)": "2026-08-15 10:00",
        "stubble  (2026-11-03 10:00)": "2026-11-03 10:00",
        "diwali   (2026-11-08 22:00)": "2026-11-08 22:00",
    }
    probe = pd.DataFrame({"ds": pd.to_datetime(list(cases.values()))})
    probe = add_event_regressors(probe)
    pred = model.predict(probe)
    print("\nlearned seasonal effects (component contributions):")
    print(f"  {'case':<28} {'yhat':>7} {'stubble':>8} {'diwali':>8}")
    for label, (_, r) in zip(cases, pred.iterrows()):
        diwali = r["diwali"] if "diwali" in r else 0.0
        print(f"  {label:<28} {r['yhat']:>7.1f} {r['stubble']:>8.1f} {diwali:>8.1f}")


def main() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL not set")
    station_id, pollutant = 30, "PM2.5"
    model, n = train(station_id, pollutant)
    print(f"trained station {station_id}/{pollutant} on {n} points")
    print(f"saved -> {artifact_path(station_id, pollutant)}")

    fc = forecast(model, hours=24)
    print(f"\n24h forecast: yhat {fc['yhat'].min():.0f}-{fc['yhat'].max():.0f} ug/m3")
    _seasonal_demo(model)


if __name__ == "__main__":
    main()
