"""Pydantic response models — the API's public data contract."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Station(BaseModel):
    id: int
    name: str
    city: str
    latitude: float | None
    longitude: float | None


class LiveReading(BaseModel):
    pollutant: str
    value: float | None       # null when the sensor reported no data
    measured_at: datetime


class StationLatest(BaseModel):
    """A station plus its single latest reading of one pollutant — powers the map."""
    id: int
    name: str
    latitude: float | None
    longitude: float | None
    value: float | None            # latest reading; null if the station has none
    measured_at: datetime | None


class HistoryPoint(BaseModel):
    measured_at: datetime
    value: float | None            # cleaned/imputed value when requested, else the raw reading
    raw: float | None = None       # original reading; only populated when clean=true
    imputed: bool = False          # True if this point was gap-filled (impute=true)


class ForecastPoint(BaseModel):
    target_time: datetime
    yhat: float | None             # predicted value
    yhat_lower: float | None       # uncertainty band
    yhat_upper: float | None
