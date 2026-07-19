"""AI-QI ML microservice (Part 3).

Small internal HTTP service the API calls for ON-DEMAND (fresh) forecasts and to
trigger (re)training. The daily batch job (services/ml/batch.py) is the primary
path that fills the forecasts table; this service is the live/optional path.

Sync endpoints on purpose: no async DB here (training uses a plain connection,
forecasting just loads a pickled model), so we avoid the Windows event-loop
dance the API service needs.

Run:  python -m services.ml.run   (serves on :8001)
"""

from __future__ import annotations

import os
import pickle

from fastapi import FastAPI, HTTPException, Query

from services.ml.train import artifact_path, forecast, train

app = FastAPI(title="AI-QI ML Service", version="0.1.0")

# Cache loaded models so we don't unpickle on every request. Refreshed on train.
_model_cache: dict[tuple[int, str], object] = {}


def _load_model(station_id: int, pollutant: str):
    key = (station_id, pollutant)
    if key not in _model_cache:
        path = artifact_path(station_id, pollutant)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            _model_cache[key] = pickle.load(f)
    return _model_cache[key]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "models_cached": len(_model_cache)}


@app.get("/forecast/{station_id}")
def get_forecast(
    station_id: int,
    pollutant: str = Query("PM2.5"),
    hours: int = Query(24, ge=1, le=168),
) -> list[dict]:
    """On-demand forecast from the station's saved model. target_time is IST."""
    model = _load_model(station_id, pollutant)
    if model is None:
        raise HTTPException(404, f"no model for station {station_id}/{pollutant}; train it first")
    fc = forecast(model, hours, station_id)
    return [
        {
            "target_time_ist": r["ds"].isoformat(),
            "yhat": float(r["yhat"]),
            "yhat_lower": float(r["yhat_lower"]),
            "yhat_upper": float(r["yhat_upper"]),
        }
        for _, r in fc.iterrows()
    ]


@app.post("/train/{station_id}")
def post_train(station_id: int, pollutant: str = Query("PM2.5")) -> dict:
    """Trigger (re)training; refreshes the cached model."""
    try:
        model, n = train(station_id, pollutant)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _model_cache[(station_id, pollutant)] = model
    return {
        "station_id": station_id,
        "pollutant": pollutant,
        "trained_points": n,
        "artifact": os.path.basename(artifact_path(station_id, pollutant)),
    }
