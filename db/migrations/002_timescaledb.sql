-- AI-QI migration 002 — convert the time-series tables to TimescaleDB hypertables.
--
-- The base schema (001_schema.sql) was written "time-first" so this conversion
-- is a drop-in. It is kept as a SEPARATE, OPT-IN migration because it REQUIRES
-- the TimescaleDB extension, which the plain `postgres:16` image does not ship.
-- Activating it is a deployment step (swap the DB image for `timescale/
-- timescaledb:*-pg16`, then run this file) — intentionally out of scope for the
-- default local stack, so it is NOT auto-run by docker-compose.
--
-- Apply (against a TimescaleDB-enabled server):
--   psql "$DATABASE_URL" -f db/migrations/002_timescaledb.sql
--
-- Idempotent: safe to re-run (guards + if_not_exists throughout).

BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- TimescaleDB requires every UNIQUE/PRIMARY KEY index to include the table's
-- partitioning (time) column. Our surrogate `id` PKs don't, so widen each PK to
-- a composite that includes the time column. The existing business-key UNIQUE
-- constraints already contain the time column, so they need no change. The `id`
-- columns stay BIGSERIAL and unique-per-chunk; nothing references them by FK
-- (only stations.id is a FK target, and stations stays a plain table).

-- readings: partition on measured_at
ALTER TABLE readings DROP CONSTRAINT IF EXISTS readings_pkey;
ALTER TABLE readings ADD PRIMARY KEY (id, measured_at);
SELECT create_hypertable('readings', 'measured_at',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => TRUE, migrate_data => TRUE);

-- weather: partition on measured_at
ALTER TABLE weather DROP CONSTRAINT IF EXISTS weather_pkey;
ALTER TABLE weather ADD PRIMARY KEY (id, measured_at);
SELECT create_hypertable('weather', 'measured_at',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => TRUE, migrate_data => TRUE);

-- forecasts: partition on target_time (the hour being forecast)
ALTER TABLE forecasts DROP CONSTRAINT IF EXISTS forecasts_pkey;
ALTER TABLE forecasts ADD PRIMARY KEY (id, target_time);
SELECT create_hypertable('forecasts', 'target_time',
                         chunk_time_interval => INTERVAL '30 days',
                         if_not_exists => TRUE, migrate_data => TRUE);

COMMIT;
