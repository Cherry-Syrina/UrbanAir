-- =====================================================================
-- UrbanAir AI — Supabase / PostgreSQL schema
-- =====================================================================
-- Apply this in the Supabase SQL Editor (Dashboard -> SQL Editor -> New
-- query) or with:  psql "$DATABASE_URL" -f database/schema.sql
--
-- This file is IDEMPOTENT: it only creates things that do not already
-- exist. It never drops a table or deletes data. It matches the SQLAlchemy
-- models in urbanair_backend/db/models.py exactly.
-- =====================================================================

-- ---------------------------------------------------------------------
-- stations: every monitored point (5 trained cities + 13 DPCC hotspots
-- + 3 landmarks). Readings/forecasts/estimates reference these.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stations (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(120) NOT NULL UNIQUE,
    station_type VARCHAR(20)  NOT NULL DEFAULT 'city',   -- city | hotspot | landmark
    city         VARCHAR(120),
    lat          DOUBLE PRECISION NOT NULL,
    lon          DOUBLE PRECISION NOT NULL,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- aqi_readings: AQI + pollutant observations.
--   source distinguishes origins: 'waqi' (ground station),
--   'owm_nowcast' (OpenWeatherMap pollutants) or 'processed_dataset'
--   (the historical CSV migration).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aqi_readings (
    id                     SERIAL PRIMARY KEY,
    station_id             INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    observed_at            TIMESTAMPTZ NOT NULL,
    aqi                    DOUBLE PRECISION,
    aqi_category           VARCHAR(32),
    co                     DOUBLE PRECISION,
    no2                    DOUBLE PRECISION,
    o3                     DOUBLE PRECISION,
    pm10                   DOUBLE PRECISION,
    pm25                   DOUBLE PRECISION,
    so2                    DOUBLE PRECISION,
    source                 VARCHAR(40) NOT NULL DEFAULT 'unknown',
    external_station_name  VARCHAR(200),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_aqi_station_time_source UNIQUE (station_id, observed_at, source)
);
CREATE INDEX IF NOT EXISTS ix_aqi_readings_station_time ON aqi_readings (station_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_aqi_readings_observed_at  ON aqi_readings (observed_at);

-- ---------------------------------------------------------------------
-- weather_readings: OpenWeatherMap current-weather snapshots.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS weather_readings (
    id            SERIAL PRIMARY KEY,
    station_id    INTEGER REFERENCES stations(id) ON DELETE CASCADE,
    city          VARCHAR(120) NOT NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    temp          DOUBLE PRECISION,
    feels_like    DOUBLE PRECISION,
    humidity      DOUBLE PRECISION,
    pressure      DOUBLE PRECISION,
    wind_speed    DOUBLE PRECISION,
    wind_deg      DOUBLE PRECISION,
    wind_gust     DOUBLE PRECISION,
    clouds_pct    DOUBLE PRECISION,
    visibility    DOUBLE PRECISION,
    weather_main  VARCHAR(64),
    weather_desc  VARCHAR(160),
    source        VARCHAR(40) NOT NULL DEFAULT 'owm',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_weather_station_time_source UNIQUE (station_id, observed_at, source)
);
CREATE INDEX IF NOT EXISTS ix_weather_readings_station_time ON weather_readings (station_id, observed_at);

-- ---------------------------------------------------------------------
-- aqi_forecasts: 24/48/72h predictions from the existing ML models.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aqi_forecasts (
    id                 SERIAL PRIMARY KEY,
    station_id         INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    generated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    target_at          TIMESTAMPTZ NOT NULL,
    horizon_hours      INTEGER NOT NULL,
    predicted_aqi      DOUBLE PRECISION NOT NULL,
    predicted_category VARCHAR(32),
    model_id           VARCHAR(80) NOT NULL DEFAULT 'forecast_lgbm',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_forecast_station_target_horizon_model
        UNIQUE (station_id, target_at, horizon_hours, model_id)
);
CREATE INDEX IF NOT EXISTS ix_forecasts_station_target ON aqi_forecasts (station_id, target_at);

-- ---------------------------------------------------------------------
-- pollution_source_estimates: rule-based source attribution snapshots
-- (one row per station per hour).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pollution_source_estimates (
    id                        SERIAL PRIMARY KEY,
    station_id                INTEGER NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    generated_at              TIMESTAMPTZ NOT NULL,
    traffic_pct               DOUBLE PRECISION,
    industrial_pct            DOUBLE PRECISION,
    construction_pct          DOUBLE PRECISION,
    waste_burning_pct         DOUBLE PRECISION,
    background_pct            DOUBLE PRECISION,
    congestion_ratio          DOUBLE PRECISION,
    wind_speed                DOUBLE PRECISION,
    wind_deg                  DOUBLE PRECISION,
    is_stubble_burning_season BOOLEAN,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_source_station_generated UNIQUE (station_id, generated_at)
);
CREATE INDEX IF NOT EXISTS ix_source_station_generated ON pollution_source_estimates (station_id, generated_at);

-- ---------------------------------------------------------------------
-- enforcement_alerts: generated enforcement intelligence alerts.
-- dedup_key prevents re-inserting the same alert within the same hour.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS enforcement_alerts (
    id                 SERIAL PRIMARY KEY,
    station_id         INTEGER REFERENCES stations(id) ON DELETE CASCADE,
    point              VARCHAR(120) NOT NULL,
    severity           VARCHAR(32)  NOT NULL,
    reason             TEXT NOT NULL,
    recommended_action TEXT,
    status             VARCHAR(20) NOT NULL DEFAULT 'open',
    priority_score     DOUBLE PRECISION,
    aqi_for_ranking    DOUBLE PRECISION,
    dedup_key          VARCHAR(255) NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_enforcement_dedup_key UNIQUE (dedup_key)
);
CREATE INDEX IF NOT EXISTS ix_enforcement_status_created ON enforcement_alerts (status, created_at);
CREATE INDEX IF NOT EXISTS ix_enforcement_station ON enforcement_alerts (station_id);
