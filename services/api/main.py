"""AI-QI backend API (Part 2).

Read-only endpoints over the ingested data. Serves RAW readings for now;
the cleaning/imputation layer is applied on read in the next step.

Run:  uvicorn services.api.main:app --reload
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import asyncio
import math
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

from services.api.aqi import overall_aqi
from services.api.cleaning import clean_series, impute_hour_of_day
from services.api.db import pool
from services.api.models import (
    AccuracyMetrics,
    AccuracyPoint,
    AccuracyResult,
    ForecastPoint,
    HistoryPoint,
    LiveReading,
    Station,
    StationLatest,
)

IST = ZoneInfo("Asia/Kolkata")   # pollution's daily cycle is local-time based
ML_SERVICE_URL = os.environ.get("ML_SERVICE_URL", "http://localhost:8001")


_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


async def _apply_schema() -> None:
    """Create tables if missing. schema.sql is fully `CREATE ... IF NOT EXISTS`,
    so this is a safe no-op after the first boot. Runs on startup because the
    Render free tier has no pre-deploy/migration hook — the app self-provisions
    its own schema against a fresh managed Postgres."""
    if not _SCHEMA_PATH.exists():
        return
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    # Strip `--` line comments FIRST, so a ';' inside a comment can't split a
    # statement, then split on ';' (safe: the schema has no procedure bodies).
    # psycopg's extended protocol runs one statement per execute.
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    statements = [s.strip() for s in no_comments.split(";") if s.strip()]
    async with pool.connection() as conn:
        for stmt in statements:
            await conn.execute(stmt)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    await _apply_schema()
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


@app.get("/ranking")
async def ranking() -> list[dict]:
    """Stations ranked by OVERALL air quality, worst first.

    Overall AQI is the CPCB definition: the max sub-index across a station's
    latest reading of each pollutant (the single worst pollutant sets the
    headline). One query pulls the latest non-null value per (station, pollutant)
    with DISTINCT ON; the AQI roll-up is done in Python via services.api.aqi.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (s.id, r.pollutant)
                       s.id, s.name, s.latitude, s.longitude,
                       r.pollutant, r.value, r.measured_at
                FROM stations s
                JOIN readings r ON r.station_id = s.id
                WHERE r.value IS NOT NULL
                ORDER BY s.id, r.pollutant, r.measured_at DESC
                """
            )
            rows = await cur.fetchall()

    # Group latest per-pollutant readings by station, then roll up to overall AQI.
    stations: dict[int, dict] = {}
    for row in rows:
        st = stations.setdefault(row["id"], {
            "id": row["id"], "name": row["name"],
            "latitude": row["latitude"], "longitude": row["longitude"],
            "readings": {}, "measured_at": row["measured_at"],
        })
        st["readings"][row["pollutant"]] = row["value"]
        if row["measured_at"] and (st["measured_at"] is None or row["measured_at"] > st["measured_at"]):
            st["measured_at"] = row["measured_at"]

    ranked = []
    for st in stations.values():
        agg = overall_aqi(st["readings"])
        if agg is None:
            continue
        aqi, category, dominant = agg
        ranked.append({
            "id": st["id"], "name": st["name"],
            "latitude": st["latitude"], "longitude": st["longitude"],
            "aqi": aqi, "category": category, "pollutant": dominant,
            "value": st["readings"][dominant], "measured_at": st["measured_at"],
        })
    ranked.sort(key=lambda s: s["aqi"], reverse=True)   # worst air on top
    return ranked


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
            neighbour_id = await _nearest_station(cur, station_id, pollutant)
            neighbour_averages = (
                await _hour_of_day_averages(cur, neighbour_id, pollutant)
                if neighbour_id is not None else {}
            )

    ist_hours = [r["measured_at"].astimezone(IST).hour for r in rows]
    filled, flags = impute_hour_of_day(
        ist_hours, cleaned, hour_averages, neighbour_averages
    )
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
                  AND target_time > now()          -- a forecast is about the future;
                  AND generated_at = (             -- excludes past-dated backtest vintages
                      SELECT max(generated_at) FROM forecasts
                      WHERE station_id = %s AND pollutant = %s
                        AND target_time > now()
                  )
                ORDER BY target_time
                """,
                (station_id, pollutant, station_id, pollutant),
            )
            return [ForecastPoint(**row) for row in await cur.fetchall()]


@app.get("/stations/{station_id}/accuracy", response_model=AccuracyResult)
async def accuracy(
    station_id: int,
    pollutant: str = Query("PM2.5", description="pollutant to score"),
    hours: int = Query(48, ge=1, le=720, description="how far back to score"),
) -> AccuracyResult:
    """Score past forecasts for a station against the readings that arrived.

    For every past target hour, takes the most recent forecast vintage and joins
    it to the actual reading at that hour, then reports MAE / RMSE / MAPE and the
    uncertainty-band coverage. Populated by the backtest job (services.ml.backtest);
    real forecasts are scored automatically as they age into the past.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if not await _station_exists(cur, station_id):
                raise HTTPException(status_code=404, detail="station not found")
            await cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (target_time)
                           target_time, yhat, yhat_lower, yhat_upper
                    FROM forecasts
                    WHERE station_id = %s AND pollutant = %s
                      AND target_time <= now()
                      AND target_time >= now() - make_interval(hours => %s)
                    ORDER BY target_time, generated_at DESC
                )
                SELECT l.target_time, l.yhat, l.yhat_lower, l.yhat_upper,
                       r.value AS actual
                FROM latest l
                JOIN readings r
                  ON r.station_id = %s AND r.pollutant = %s
                 AND r.measured_at = l.target_time
                ORDER BY l.target_time
                """,
                (station_id, pollutant, hours, station_id, pollutant),
            )
            rows = await cur.fetchall()

    points = [AccuracyPoint(**row) for row in rows]
    return AccuracyResult(metrics=_score(rows), points=points)


def _score(rows: list[dict]) -> AccuracyMetrics:
    """Aggregate error metrics over rows that have both a prediction and an actual."""
    pairs = [(r["yhat"], r["actual"], r["yhat_lower"], r["yhat_upper"])
             for r in rows if r["yhat"] is not None and r["actual"] is not None]
    n = len(pairs)
    if n == 0:
        return AccuracyMetrics(n=0, mae=None, rmse=None, mape=None, coverage=None)

    abs_err = [abs(y - a) for y, a, _, _ in pairs]
    sq_err = [(y - a) ** 2 for y, a, _, _ in pairs]
    pct_err = [abs(y - a) / abs(a) for y, a, _, _ in pairs if a]
    in_band = [
        1 for y, a, lo, hi in pairs
        if lo is not None and hi is not None and lo <= a <= hi
    ]
    return AccuracyMetrics(
        n=n,
        mae=round(sum(abs_err) / n, 2),
        rmse=round(math.sqrt(sum(sq_err) / n), 2),
        mape=round(100 * sum(pct_err) / len(pct_err), 1) if pct_err else None,
        coverage=round(100 * len(in_band) / n, 1),
    )


async def _nearest_station(cur, station_id: int, pollutant: str) -> int | None:
    """Closest OTHER station (by coordinates) that has data for this pollutant.

    Used as the spatial fallback for imputation. Squared Euclidean distance on
    lat/long is fine at city scale (Delhi); no need for great-circle accuracy.
    Returns None if the station has no coordinates or no candidate neighbour.
    """
    await cur.execute(
        """
        SELECT n.id
        FROM stations s
        JOIN stations n ON n.id <> s.id
                        AND n.latitude IS NOT NULL AND n.longitude IS NOT NULL
        WHERE s.id = %s AND s.latitude IS NOT NULL AND s.longitude IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM readings r
              WHERE r.station_id = n.id AND r.pollutant = %s AND r.value IS NOT NULL
          )
        ORDER BY (n.latitude - s.latitude) ^ 2 + (n.longitude - s.longitude) ^ 2
        LIMIT 1
        """,
        (station_id, pollutant),
    )
    row = await cur.fetchone()
    return row["id"] if row else None


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
