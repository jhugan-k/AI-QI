"""AI-QI backend API (Part 2).

Read-only endpoints over the ingested data. Serves RAW readings for now;
the cleaning/imputation layer is applied on read in the next step.

Run:  uvicorn services.api.main:app --reload
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

# Windows defaults to ProactorEventLoop, but psycopg3's async mode requires the
# SelectorEventLoop. Must be set before the event loop is created. No-op on
# Linux (production), so it's guarded to win32.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

from services.api.cleaning import clean_series, impute_hour_of_day
from services.api.db import pool
from services.api.models import (
    ForecastPoint,
    HistoryPoint,
    LiveReading,
    Station,
    StationLatest,
)

IST = ZoneInfo("Asia/Kolkata")   # pollution's daily cycle is local-time based
ML_SERVICE_URL = os.environ.get("ML_SERVICE_URL", "http://localhost:8001")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    yield
    await pool.close()


app = FastAPI(title="AI-QI API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Liveness + DB connectivity check."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
            await cur.fetchone()
    return {"status": "ok", "db": "ok"}


@app.get("/stations", response_model=list[Station])
async def list_stations() -> list[Station]:
    """All known monitoring stations."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, name, city, latitude, longitude "
                "FROM stations ORDER BY name"
            )
            return [Station(**row) for row in await cur.fetchall()]


@app.get("/overview", response_model=list[StationLatest])
async def overview(
    pollutant: str = Query("PM2.5", description="pollutant to summarise per station"),
) -> list[StationLatest]:
    """Every station with its single latest reading of one pollutant.

    One round-trip for the whole map: DISTINCT ON (station) + ORDER BY newest
    picks the latest matching reading per station. LEFT JOIN keeps stations that
    have no reading for this pollutant (value/measured_at come back null).
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (s.id)
                       s.id, s.name, s.latitude, s.longitude,
                       r.value, r.measured_at
                FROM stations s
                LEFT JOIN readings r
                       ON r.station_id = s.id AND r.pollutant = %s
                ORDER BY s.id, r.measured_at DESC NULLS LAST
                """,
                (pollutant,),
            )
            return [StationLatest(**row) for row in await cur.fetchall()]


async def _station_exists(cur, station_id: int) -> bool:
    await cur.execute("SELECT 1 FROM stations WHERE id = %s", (station_id,))
    return await cur.fetchone() is not None


@app.get("/stations/{station_id}/live", response_model=list[LiveReading])
async def live(station_id: int) -> list[LiveReading]:
    """Latest reading for each pollutant at a station.

    DISTINCT ON (pollutant) + ORDER BY pollutant, measured_at DESC returns
    exactly one (the newest) row per pollutant in a single query.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if not await _station_exists(cur, station_id):
                raise HTTPException(status_code=404, detail="station not found")
            await cur.execute(
                """
                SELECT DISTINCT ON (pollutant) pollutant, value, measured_at
                FROM readings
                WHERE station_id = %s
                ORDER BY pollutant, measured_at DESC
                """,
                (station_id,),
            )
            return [LiveReading(**row) for row in await cur.fetchall()]


@app.get("/stations/{station_id}/history", response_model=list[HistoryPoint])
async def history(
    station_id: int,
    pollutant: str = Query(..., description="e.g. PM2.5, PM10, NO2, SO2, CO, OZONE"),
    hours: int = Query(24, ge=1, le=720, description="lookback window in hours"),
    clean: bool = Query(False, description="apply clean-on-read (validate, de-outlier, smooth)"),
    impute: bool = Query(False, description="fill gaps from hour-of-day history (implies clean)"),
) -> list[HistoryPoint]:
    """Time series of one pollutant at a station over the last N hours.

    - clean=true: `value` is the cleaned series, `raw` is the original reading.
    - impute=true: also fills gaps from the station's hour-of-day averages and
      marks those points `imputed`. Implies clean.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if not await _station_exists(cur, station_id):
                raise HTTPException(status_code=404, detail="station not found")
            await cur.execute(
                """
                SELECT measured_at, value
                FROM readings
                WHERE station_id = %s
                  AND pollutant = %s
                  AND measured_at >= now() - make_interval(hours => %s)
                ORDER BY measured_at
                """,
                (station_id, pollutant, hours),
            )
            rows = await cur.fetchall()

            if not clean and not impute:
                return [
                    HistoryPoint(measured_at=r["measured_at"], value=r["value"])
                    for r in rows
                ]

            cleaned = clean_series(pollutant, [r["value"] for r in rows])

            if not impute:
                return [
                    HistoryPoint(measured_at=r["measured_at"], value=c, raw=r["value"])
                    for r, c in zip(rows, cleaned)
                ]

            hour_averages = await _hour_of_day_averages(cur, station_id, pollutant)

    ist_hours = [r["measured_at"].astimezone(IST).hour for r in rows]
    filled, flags = impute_hour_of_day(ist_hours, cleaned, hour_averages)
    return [
        HistoryPoint(measured_at=r["measured_at"], value=v, raw=r["value"], imputed=f)
        for r, v, f in zip(rows, filled, flags)
    ]


@app.get("/stations/{station_id}/forecast/live")
async def forecast_live(station_id: int, pollutant: str = Query("PM2.5")) -> list[dict]:
    """On-demand fresh forecast, fetched from the ML microservice over HTTP.

    Demonstrates the API -> ML internal call. Degrades gracefully: 503 if the ML
    service is unreachable (the stored /forecast endpoint is the reliable path).
    """
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            resp = await client.get(
                f"{ML_SERVICE_URL}/forecast/{station_id}",
                params={"pollutant": pollutant},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(status_code=exc.response.status_code, detail="ML service error")
        except httpx.RequestError:
            raise HTTPException(status_code=503, detail="ML service unavailable")
    return resp.json()


@app.get("/stations/{station_id}/forecast", response_model=list[ForecastPoint])
async def station_forecast(
    station_id: int,
    pollutant: str = Query("PM2.5", description="pollutant to forecast"),
) -> list[ForecastPoint]:
    """Latest stored 24h forecast for a station, produced by the ML batch job.

    Serves the most recent vintage (max generated_at) straight from the
    forecasts table — fast, and works even if the ML service is offline.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if not await _station_exists(cur, station_id):
                raise HTTPException(status_code=404, detail="station not found")
            await cur.execute(
                """
                SELECT target_time, yhat, yhat_lower, yhat_upper
                FROM forecasts
                WHERE station_id = %s AND pollutant = %s
                  AND generated_at = (
                      SELECT max(generated_at) FROM forecasts
                      WHERE station_id = %s AND pollutant = %s
                  )
                ORDER BY target_time
                """,
                (station_id, pollutant, station_id, pollutant),
            )
            return [ForecastPoint(**row) for row in await cur.fetchall()]


async def _hour_of_day_averages(cur, station_id: int, pollutant: str) -> dict[int, float]:
    """Mean value per IST hour-of-day over the last 30 days, for imputation."""
    await cur.execute(
        """
        SELECT EXTRACT(HOUR FROM measured_at AT TIME ZONE 'Asia/Kolkata')::int AS hod,
               avg(value) AS avg_value
        FROM readings
        WHERE station_id = %s
          AND pollutant = %s
          AND value IS NOT NULL
          AND measured_at >= now() - interval '30 days'
        GROUP BY hod
        """,
        (station_id, pollutant),
    )
    return {row["hod"]: float(row["avg_value"]) for row in await cur.fetchall()}


# --- Static web dashboard (Part 4) -------------------------------------------
# Served same-origin so the frontend's fetch() calls hit /stations, /history,
# etc. with no CORS. Mounted LAST and at "/" so the greedy catch-all doesn't
# shadow the explicit API routes above (Starlette matches routes in order).
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"
if _WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
