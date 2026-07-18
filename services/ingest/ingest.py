"""AI-QI ingest job (Part 1).

Pulls Delhi's live air-quality readings from the CPCB feed on data.gov.in and
writes them into Postgres. Designed to be idempotent: safe to run every hour,
never creates duplicate readings.

Run:  python -m services.ingest.ingest      (from repo root, venv active)
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import psycopg
from dotenv import load_dotenv

load_dotenv()

# --- Config -----------------------------------------------------------------

RESOURCE_ID = "3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
API_URL = f"https://api.data.gov.in/resource/{RESOURCE_ID}"
PAGE_SIZE = 500                       # records per request; the gov API slows down on huge pages
REQUEST_TIMEOUT = 60                  # data.gov.in is slow/flaky; be patient
MAX_RETRIES = 4                       # transient timeouts are common on this upstream
IST = ZoneInfo("Asia/Kolkata")       # CPCB timestamps are in India Standard Time

DATABASE_URL = os.environ.get("DATABASE_URL")
CPCB_API_KEY = os.environ.get("CPCB_API_KEY")


# --- Fetch ------------------------------------------------------------------

def _get_with_retry(client: httpx.Client, params: dict) -> httpx.Response:
    """GET with exponential backoff — this gov API times out intermittently."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(API_URL, params=params)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt          # 2s, 4s, 8s
            print(f"  request failed ({exc.__class__.__name__}), retry {attempt}/{MAX_RETRIES - 1} in {wait}s")
            time.sleep(wait)


def fetch_delhi_records() -> list[dict]:
    """Page through the CPCB API, returning all records for state=Delhi."""
    records: list[dict] = []
    offset = 0
    # NB: data.gov.in's WAF silently hangs (never responds) on the default
    # "python-httpx" User-Agent. Any identifying UA works. Do not remove.
    headers = {"User-Agent": "AI-QI-ingest/1.0"}
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
        while True:
            resp = _get_with_retry(
                client,
                {
                    "api-key": CPCB_API_KEY,
                    "format": "json",
                    "filters[state]": "Delhi",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
            )
            batch = resp.json().get("records", [])
            records.extend(batch)
            if len(batch) < PAGE_SIZE:
                break                # last page reached
            offset += PAGE_SIZE
    return records


# --- Parse helpers ----------------------------------------------------------

def parse_value(raw: str | None) -> float | None:
    """CPCB reports 'NA' (and sometimes blanks) when a sensor has no data."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "" or raw.upper() == "NA":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_timestamp(raw: str) -> datetime:
    """CPCB last_update looks like '17-07-2026 21:00:00', in IST."""
    naive = datetime.strptime(raw.strip(), "%d-%m-%Y %H:%M:%S")
    return naive.replace(tzinfo=IST)


def parse_float(raw: str | None) -> float | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# --- Load -------------------------------------------------------------------

def upsert_stations(conn: psycopg.Connection, records: list[dict]) -> dict[str, int]:
    """Insert any new stations, then return a {station_name: station_id} map.

    Dedup is on stations.name (the stable CPCB identifier). ON CONFLICT keeps
    lat/lon fresh in case a station's coordinates are later corrected upstream.
    """
    # One row per distinct station (records are per-pollutant, so collapse first).
    stations: dict[str, dict] = {}
    for r in records:
        name = r["station"]
        if name not in stations:
            stations[name] = r

    with conn.cursor() as cur:
        for name, r in stations.items():
            cur.execute(
                """
                INSERT INTO stations (name, city, state, latitude, longitude)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                    SET latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude
                """,
                (
                    name,
                    r.get("city") or "Delhi",
                    r.get("state") or "Delhi",
                    parse_float(r.get("latitude")),
                    parse_float(r.get("longitude")),
                ),
            )
        cur.execute("SELECT id, name FROM stations")
        return {name: sid for sid, name in cur.fetchall()}


def insert_readings(
    conn: psycopg.Connection, records: list[dict], name_to_id: dict[str, int]
) -> int:
    """Insert readings idempotently. Returns number of NEW rows written."""
    rows = []
    for r in records:
        station_id = name_to_id.get(r["station"])
        if station_id is None:
            continue
        rows.append(
            (
                station_id,
                r["pollutant_id"],
                parse_value(r.get("avg_value")),
                parse_timestamp(r["last_update"]),
            )
        )

    inserted = 0
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO readings (station_id, pollutant, value, measured_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (station_id, pollutant, measured_at) DO NOTHING
                """,
                row,
            )
            inserted += cur.rowcount     # 1 if inserted, 0 if it was a duplicate
    return inserted


# --- Entry point ------------------------------------------------------------

def run_ingest() -> tuple[int, int]:
    """Run one full ingest cycle. Returns (stations_known, new_readings).

    This is the unit the scheduler (or a Render cron job) calls. It raises on
    failure so the caller can decide how to handle it.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set — copy .env.example to .env")
    if not CPCB_API_KEY:
        raise RuntimeError("CPCB_API_KEY not set — register at https://data.gov.in and add it to .env")

    records = fetch_delhi_records()
    with psycopg.connect(DATABASE_URL) as conn:
        name_to_id = upsert_stations(conn, records)
        inserted = insert_readings(conn, records, name_to_id)
        conn.commit()
    return len(name_to_id), inserted


def main() -> None:
    try:
        stations, inserted = run_ingest()
    except RuntimeError as exc:
        sys.exit(str(exc))
    print(f"Stations known: {stations}")
    print(f"New readings inserted: {inserted} (duplicates skipped)")


if __name__ == "__main__":
    main()
