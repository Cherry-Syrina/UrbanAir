"""
SQLAlchemy ORM models for UrbanAir AI.

Tables (see db/schema.sql for the canonical Postgres DDL):

  stations                      — monitored points (cities, hotspots, landmarks)
  aqi_readings                  — AQI + pollutant observations per station
  weather_readings              — weather observations per city/station
  aqi_forecasts                 — 24/48/72h model forecasts
  pollution_source_estimates    — rule-based source attribution snapshots
  enforcement_alerts            — generated enforcement alerts

Only columns that correspond to data the existing project actually
produces are defined — nothing speculative.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from .database import Base


class Station(Base):
    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), unique=True, nullable=False)
    station_type = Column(String(20), nullable=False, default="city")  # city|hotspot|landmark
    city = Column(String(120), nullable=True)  # nearest trained city, if any
    lat = Column(Float, nullable=False)
    lon = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    aqi_readings = relationship("AqiReading", back_populates="station")
    weather_readings = relationship("WeatherReading", back_populates="station")


class AqiReading(Base):
    __tablename__ = "aqi_readings"
    __table_args__ = (
        UniqueConstraint("station_id", "observed_at", "source", name="uq_aqi_station_time_source"),
        Index("ix_aqi_readings_station_time", "station_id", "observed_at"),
        Index("ix_aqi_readings_observed_at", "observed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    aqi = Column(Float, nullable=True)
    aqi_category = Column(String(32), nullable=True)
    co = Column(Float, nullable=True)
    no2 = Column(Float, nullable=True)
    o3 = Column(Float, nullable=True)
    pm10 = Column(Float, nullable=True)
    pm25 = Column(Float, nullable=True)
    so2 = Column(Float, nullable=True)
    source = Column(String(40), nullable=False, default="unknown")  # waqi|owm_nowcast|processed_dataset
    external_station_name = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    station = relationship("Station", back_populates="aqi_readings")


class WeatherReading(Base):
    __tablename__ = "weather_readings"
    __table_args__ = (
        UniqueConstraint("station_id", "observed_at", "source", name="uq_weather_station_time_source"),
        Index("ix_weather_readings_station_time", "station_id", "observed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=True)
    city = Column(String(120), nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    temp = Column(Float, nullable=True)
    feels_like = Column(Float, nullable=True)
    humidity = Column(Float, nullable=True)
    pressure = Column(Float, nullable=True)
    wind_speed = Column(Float, nullable=True)
    wind_deg = Column(Float, nullable=True)
    wind_gust = Column(Float, nullable=True)
    clouds_pct = Column(Float, nullable=True)
    visibility = Column(Float, nullable=True)
    weather_main = Column(String(64), nullable=True)
    weather_desc = Column(String(160), nullable=True)
    source = Column(String(40), nullable=False, default="owm")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    station = relationship("Station", back_populates="weather_readings")


class AqiForecast(Base):
    __tablename__ = "aqi_forecasts"
    __table_args__ = (
        UniqueConstraint("station_id", "target_at", "horizon_hours", "model_id",
                         name="uq_forecast_station_target_horizon_model"),
        Index("ix_forecasts_station_target", "station_id", "target_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    generated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    target_at = Column(DateTime(timezone=True), nullable=False)
    horizon_hours = Column(Integer, nullable=False)
    predicted_aqi = Column(Float, nullable=False)
    predicted_category = Column(String(32), nullable=True)
    model_id = Column(String(80), nullable=False, default="forecast_lgbm")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PollutionSourceEstimate(Base):
    __tablename__ = "pollution_source_estimates"
    __table_args__ = (
        UniqueConstraint("station_id", "generated_at", name="uq_source_station_generated"),
        Index("ix_source_station_generated", "station_id", "generated_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    generated_at = Column(DateTime(timezone=True), nullable=False)
    traffic_pct = Column(Float, nullable=True)
    industrial_pct = Column(Float, nullable=True)
    construction_pct = Column(Float, nullable=True)
    waste_burning_pct = Column(Float, nullable=True)
    background_pct = Column(Float, nullable=True)
    congestion_ratio = Column(Float, nullable=True)
    wind_speed = Column(Float, nullable=True)
    wind_deg = Column(Float, nullable=True)
    is_stubble_burning_season = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class EnforcementAlert(Base):
    __tablename__ = "enforcement_alerts"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_enforcement_dedup_key"),
        Index("ix_enforcement_status_created", "status", "created_at"),
        Index("ix_enforcement_station", "station_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=True)
    point = Column(String(120), nullable=False)
    severity = Column(String(32), nullable=False)  # AQI category band
    reason = Column(Text, nullable=False)
    recommended_action = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="open")  # open|acknowledged|closed
    priority_score = Column(Float, nullable=True)
    aqi_for_ranking = Column(Float, nullable=True)
    dedup_key = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
