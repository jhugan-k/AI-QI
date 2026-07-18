-- AI-QI schema (Part 1) — plain PostgreSQL 16.
-- TimescaleDB hypertable conversion is deferred to a later part; this schema
-- is written so that conversion is a drop-in change (readings is time-first).

-- One row per physical CPCB monitoring station in Delhi (~40 rows total).
CREATE TABLE IF NOT EXISTS stations (
    id           SERIAL PRIMARY KEY,
    station_code TEXT UNIQUE,              -- reserved for a real CPCB code; API doesn't supply one yet
    name         TEXT NOT NULL UNIQUE,     -- the stable identifier from CPCB, e.g. "Anand Vihar, Delhi - DPCC"
    city         TEXT NOT NULL DEFAULT 'Delhi',
    state        TEXT NOT NULL DEFAULT 'Delhi',
    latitude     DOUBLE PRECISION,
    longitude    DOUBLE PRECISION,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Time-series pollutant readings in LONG format: one row per
-- (station, pollutant, timestamp). This mirrors the CPCB API payload
-- exactly, so ingest is a straight insert with no pivoting.
CREATE TABLE IF NOT EXISTS readings (
    id          BIGSERIAL PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    pollutant   TEXT NOT NULL,             -- PM2.5, PM10, NO2, SO2, CO, OZONE
    value       DOUBLE PRECISION,          -- nullable: sensor can report no data
    measured_at TIMESTAMPTZ NOT NULL,      -- CPCB's own timestamp for the reading
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Dedup guard: re-running the hourly ingest must not create duplicates.
    -- Lets us use INSERT ... ON CONFLICT DO NOTHING in the ingest job.
    CONSTRAINT readings_unique UNIQUE (station_id, pollutant, measured_at)
);

-- Primary query pattern: "latest readings for station X" and
-- "history of pollutant P at station X over a time window".
CREATE INDEX IF NOT EXISTS readings_station_time_idx
    ON readings (station_id, measured_at DESC);

CREATE INDEX IF NOT EXISTS readings_station_pollutant_time_idx
    ON readings (station_id, pollutant, measured_at DESC);

-- Meteorology per station (from OpenWeather). Unlike pollutants, the weather
-- fields are a fixed known set, so this table is WIDE (one row per snapshot).
-- These become Prophet regressors in Part 3 (weather drives pollution dispersal).
CREATE TABLE IF NOT EXISTS weather (
    id          BIGSERIAL PRIMARY KEY,
    station_id  INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    measured_at TIMESTAMPTZ NOT NULL,      -- OpenWeather 'dt' (UTC)
    temp_c      DOUBLE PRECISION,
    humidity    INTEGER,                   -- %
    pressure    INTEGER,                   -- hPa
    wind_speed  DOUBLE PRECISION,          -- m/s
    wind_deg    INTEGER,                   -- degrees
    clouds      INTEGER,                   -- %
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT weather_unique UNIQUE (station_id, measured_at)
);

CREATE INDEX IF NOT EXISTS weather_station_time_idx
    ON weather (station_id, measured_at DESC);

-- Model predictions. target_time = the hour being forecast; generated_at = when
-- the forecast was produced (its "vintage"). Both are needed so Part 5 can score
-- "the forecast made yesterday for today" against the actual reading.
CREATE TABLE IF NOT EXISTS forecasts (
    id            BIGSERIAL PRIMARY KEY,
    station_id    INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    pollutant     TEXT NOT NULL,
    target_time   TIMESTAMPTZ NOT NULL,
    yhat          DOUBLE PRECISION,
    yhat_lower    DOUBLE PRECISION,
    yhat_upper    DOUBLE PRECISION,
    model_version TEXT,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT forecasts_unique UNIQUE (station_id, pollutant, target_time, generated_at)
);

CREATE INDEX IF NOT EXISTS forecasts_station_target_idx
    ON forecasts (station_id, pollutant, target_time DESC);
