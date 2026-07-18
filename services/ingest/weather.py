"""AI-QI weather ingest (Part 1).

For each known station (with coordinates), pull current meteorology from
OpenWeather and store it in the `weather` table. Idempotent on
(station_id, measured_at). Weather becomes a Prophet regressor in Part 3.

Run:  python -m services.ingest.weather
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import httpx
import psycopg
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://api.openweathermap.org/data/2.5/weather"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4

DATABASE_URL = os.environ.get("DATABASE_URL")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")


def _get_with_retry(client: httpx.Client, params: dict) -> httpx.Response:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(API_URL, params=params)
            resp.raise_for_status()
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == MAX_RETRIES:
                raise
            time.sleep(2 ** attempt)


def fetch_weather(client: httpx.Client, lat: float, lon: float) -> dict:
    resp = _get_with_retry(
        client,
        {
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",   # temp in Celsius, wind in m/s
        },
    )
    return resp.json()


def parse_weather(payload: dict) -> dict:
    """Flatten OpenWeather's nested current-weather payload to our columns."""
    main = payload.get("main", {})
    wind = payload.get("wind", {})
    clouds = payload.get("clouds", {})
    dt = payload.get("dt")  # unix seconds, UTC
    measured_at = datetime.fromtimestamp(dt, tz=timezone.utc) if dt else datetime.now(timezone.utc)
    return {
        "measured_at": measured_at,
        "temp_c": main.get("temp"),
        "humidity": main.get("humidity"),
        "pressure": main.get("pressure"),
        "wind_speed": wind.get("speed"),
        "wind_deg": wind.get("deg"),
        "clouds": clouds.get("all"),
    }


def run_weather_ingest() -> tuple[int, int]:
    """Poll weather for every station with coordinates. Returns (polled, inserted).

    If no OpenWeather key is configured, logs nothing and returns (0, 0) so the
    caller can treat weather as optional without failing the whole cycle.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    if not OPENWEATHER_API_KEY:
        return 0, 0  # weather is optional until a key is provided

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, latitude, longitude FROM stations "
                "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
            )
            stations = cur.fetchall()

        polled = 0
        inserted = 0
        headers = {"User-Agent": "AI-QI-ingest/1.0"}
        with httpx.Client(timeout=REQUEST_TIMEOUT, headers=headers) as client:
            with conn.cursor() as cur:
                for station_id, lat, lon in stations:
                    w = parse_weather(fetch_weather(client, lat, lon))
                    polled += 1
                    cur.execute(
                        """
                        INSERT INTO weather (station_id, measured_at, temp_c,
                                             humidity, pressure, wind_speed,
                                             wind_deg, clouds)
                        VALUES (%(sid)s, %(measured_at)s, %(temp_c)s, %(humidity)s,
                                %(pressure)s, %(wind_speed)s, %(wind_deg)s, %(clouds)s)
                        ON CONFLICT (station_id, measured_at) DO NOTHING
                        """,
                        {"sid": station_id, **w},
                    )
                    inserted += cur.rowcount
        conn.commit()
    return polled, inserted


def main() -> None:
    if not OPENWEATHER_API_KEY:
        sys.exit("OPENWEATHER_API_KEY not set — register at https://openweathermap.org and add it to .env")
    polled, inserted = run_weather_ingest()
    print(f"Stations polled: {polled}")
    print(f"New weather rows inserted: {inserted} (duplicates skipped)")


if __name__ == "__main__":
    main()
