"""
Repository layer: thin, parameterized CRUD helpers over the ORM models.

All functions take an explicit SQLAlchemy Session so the caller (service.py)
controls transaction boundaries. None of them commit unless asked.
Duplicate prevention is handled by unique constraints + insert-ignore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from . import models


# --------------------------------------------------------------------------- #
# Stations
# --------------------------------------------------------------------------- #
def ensure_station(session, name: str, lat: float, lon: float,
                   station_type: str = "city", city: Optional[str] = None) -> int:
    """Fetch a station by name, creating it if missing. Returns its id."""
    station = session.execute(
        select(models.Station).where(models.Station.name == name)
    ).scalar_one_or_none()
    if station is None:
        station = models.Station(name=name, lat=lat, lon=lon,
                                 station_type=station_type, city=city or name)
        session.add(station)
        session.flush()
    return station.id


def ensure_stations(session, points: dict, station_type: str = "city") -> dict:
    """points: {name: (lat, lon)}. Returns {name: station_id}."""
    mapping = {}
    for name, coords in points.items():
        if not coords:
            continue
        lat, lon = coords
        mapping[name] = ensure_station(session, name, lat, lon, station_type=station_type)
    return mapping


# --------------------------------------------------------------------------- #
# Generic insert-ignore (duplicate prevention, dialect-aware)
# --------------------------------------------------------------------------- #
def _insert_ignore_pg(session, model, rows: list) -> int:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(model).values(rows).on_conflict_do_nothing()
    result = session.execute(stmt)
    return result.rowcount if result.rowcount is not None else 0


def _insert_ignore_generic(session, model, rows: list) -> int:
    inserted = 0
    for row in rows:
        savepoint = session.begin_nested()
        try:
            session.add(model(**row))
            session.flush()
            savepoint.commit()
            inserted += 1
        except IntegrityError:
            savepoint.rollback()
    return inserted


def bulk_insert_ignore(session, model, rows: Iterable[dict]) -> int:
    """Insert rows, skipping any that violate a unique constraint."""
    rows = list(rows)
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        # Chunk so a single statement doesn't get unreasonably large.
        total = 0
        chunk_size = 1000
        for i in range(0, len(rows), chunk_size):
            total += _insert_ignore_pg(session, model, rows[i:i + chunk_size])
        return total
    return _insert_ignore_generic(session, model, rows)


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def save_aqi_readings(session, rows: list) -> int:
    return bulk_insert_ignore(session, models.AqiReading, rows)


def save_weather_readings(session, rows: list) -> int:
    return bulk_insert_ignore(session, models.WeatherReading, rows)


def save_forecasts(session, rows: list) -> int:
    return bulk_insert_ignore(session, models.AqiForecast, rows)


def save_source_estimates(session, rows: list) -> int:
    return bulk_insert_ignore(session, models.PollutionSourceEstimate, rows)


def save_enforcement_alerts(session, rows: list) -> int:
    return bulk_insert_ignore(session, models.EnforcementAlert, rows)


# --------------------------------------------------------------------------- #
# Latest-only "snapshot" upserts (demo mode)
#
# The demo keeps only the CURRENT reading per station instead of a growing
# history, so a new reading UPDATES the existing row rather than adding one.
# No schema change is required: the row is located by its natural key
# (station_id + source, or city + source for weather without a station).
# --------------------------------------------------------------------------- #
def _upsert_snapshot(session, model, key_values: dict, values: dict) -> str:
    """Update the single row matching key_values, or insert it.

    If more than one row already matches (e.g. leftover history), the newest
    is kept and updated and the extras are removed, so the table converges to
    exactly one row per key. Returns "inserted" or "updated".
    """
    criteria = [getattr(model, col) == val for col, val in key_values.items()]
    session.flush()  # sessionmaker uses autoflush=False; make pending adds visible
    rows = session.execute(select(model).where(*criteria)).scalars().all()
    if not rows:
        session.add(model(**{**key_values, **values}))
        return "inserted"
    keep = rows[0]
    for col, val in values.items():
        setattr(keep, col, val)
    for extra in rows[1:]:
        session.delete(extra)
    return "updated"


def upsert_aqi_reading(session, station_id: int, source: str, values: dict) -> str:
    """Keep exactly one aqi_readings row per (station_id, source)."""
    return _upsert_snapshot(
        session, models.AqiReading,
        {"station_id": station_id, "source": source}, values,
    )


def upsert_weather_reading(session, station_id: Optional[int], city: str,
                           source: str, values: dict) -> str:
    """Keep exactly one weather_readings row per station (or city) + source.

    `city` is always part of the key (the column is NOT NULL) and is added to
    the inserted row; `station_id` may be None for cities without a station.
    """
    keys = {"station_id": station_id, "city": city, "source": source}
    return _upsert_snapshot(session, models.WeatherReading, keys, values)


def prune_before(session, model, column, cutoff) -> int:
    """Delete rows whose `column` is older than `cutoff`. Returns row count."""
    result = session.execute(delete(model).where(column < cutoff))
    return int(result.rowcount or 0)


def prune_forecasts(session, days: int = 7) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return prune_before(session, models.AqiForecast,
                        models.AqiForecast.generated_at, cutoff)


def prune_source_estimates(session, days: int = 7) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return prune_before(session, models.PollutionSourceEstimate,
                        models.PollutionSourceEstimate.generated_at, cutoff)


def prune_alerts(session, days: int = 7) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return prune_before(session, models.EnforcementAlert,
                        models.EnforcementAlert.created_at, cutoff)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def get_aqi_history(session, station_name: str, hours: int, limit: int, offset: int) -> list:
    since = datetime.now(timezone.utc) - _hours(hours)
    stmt = (
        select(models.AqiReading)
        .join(models.Station, models.AqiReading.station_id == models.Station.id)
        .where(models.Station.name == station_name)
        .where(models.AqiReading.observed_at >= since)
        .order_by(models.AqiReading.observed_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return [_aqi_to_dict(r) for r in session.execute(stmt).scalars().all()]


def count_aqi_history(session, station_name: str, hours: int) -> int:
    from sqlalchemy import func as sa_func

    since = datetime.now(timezone.utc) - _hours(hours)
    stmt = (
        select(sa_func.count(models.AqiReading.id))
        .join(models.Station, models.AqiReading.station_id == models.Station.id)
        .where(models.Station.name == station_name)
        .where(models.AqiReading.observed_at >= since)
    )
    return int(session.execute(stmt).scalar() or 0)


def get_latest_forecasts(session, station_name: str) -> list:
    stmt = (
        select(models.AqiForecast)
        .join(models.Station, models.AqiForecast.station_id == models.Station.id)
        .where(models.Station.name == station_name)
        .order_by(models.AqiForecast.generated_at.desc(),
                  models.AqiForecast.horizon_hours.asc())
        .limit(12)
    )
    return [_forecast_to_dict(r) for r in session.execute(stmt).scalars().all()]


def get_alerts(session, status: Optional[str], limit: int, offset: int) -> list:
    stmt = select(models.EnforcementAlert).order_by(models.EnforcementAlert.created_at.desc())
    if status:
        stmt = stmt.where(models.EnforcementAlert.status == status)
    stmt = stmt.limit(limit).offset(offset)
    return [_alert_to_dict(r) for r in session.execute(stmt).scalars().all()]


def list_stations(session) -> list:
    stmt = select(models.Station).order_by(models.Station.name.asc())
    return [
        {"name": s.name, "station_type": s.station_type, "city": s.city,
         "lat": s.lat, "lon": s.lon}
        for s in session.execute(stmt).scalars().all()
    ]


def get_latest_reading(session, station_name: str) -> Optional[dict]:
    """Most recent stored AQI observation for a station (no time window).

    Used as a read-only fallback when no live collector CSV is present.
    Returns None when the station has no readings."""
    stmt = (
        select(models.AqiReading)
        .join(models.Station, models.AqiReading.station_id == models.Station.id)
        .where(models.Station.name == station_name)
        .order_by(models.AqiReading.observed_at.desc())
        .limit(1)
    )
    row = session.execute(stmt).scalars().first()
    return _aqi_to_dict(row) if row is not None else None


def get_recent_readings(session, station_name: str, limit: int) -> list:
    """Latest `limit` stored observations for a station, newest first.

    No lower time bound (unlike get_aqi_history) so the historical 2019-2024
    dataset is still reachable once the live window has moved past it."""
    stmt = (
        select(models.AqiReading)
        .join(models.Station, models.AqiReading.station_id == models.Station.id)
        .where(models.Station.name == station_name)
        .order_by(models.AqiReading.observed_at.desc())
        .limit(max(1, int(limit)))
    )
    return [_aqi_to_dict(r) for r in session.execute(stmt).scalars().all()]


def get_latest_source_estimates(session) -> list:
    """Most recent pollution_source_estimates row per station (all points)."""
    from sqlalchemy import func as sa_func

    latest = (
        select(
            models.PollutionSourceEstimate.station_id,
            sa_func.max(models.PollutionSourceEstimate.generated_at).label("mx"),
        )
        .group_by(models.PollutionSourceEstimate.station_id)
        .subquery()
    )
    stmt = (
        select(models.PollutionSourceEstimate, models.Station.name)
        .join(models.Station, models.PollutionSourceEstimate.station_id == models.Station.id)
        .join(
            latest,
            (models.PollutionSourceEstimate.station_id == latest.c.station_id)
            & (models.PollutionSourceEstimate.generated_at == latest.c.mx),
        )
    )
    return [_source_to_dict(est, name) for est, name in session.execute(stmt).all()]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _hours(h: int):
    from datetime import timedelta

    return timedelta(hours=max(1, int(h)))


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _aqi_to_dict(r: models.AqiReading) -> dict:
    return {
        "station": r.station.name if r.station else None,
        "timestamp": _iso(r.observed_at),
        "aqi": r.aqi,
        "category": r.aqi_category,
        "co": r.co, "no2": r.no2, "o3": r.o3,
        "pm10": r.pm10, "pm25": r.pm25, "so2": r.so2,
        "source": r.source,
    }


def _forecast_to_dict(r: models.AqiForecast) -> dict:
    return {
        "station": r.station.name if hasattr(r, "station") and r.station else None,
        "generated_at": _iso(r.generated_at),
        "target_at": _iso(r.target_at),
        "horizon_hours": r.horizon_hours,
        "predicted_aqi": r.predicted_aqi,
        "predicted_category": r.predicted_category,
        "model_id": r.model_id,
    }


def _alert_to_dict(r: models.EnforcementAlert) -> dict:
    return {
        "id": r.id,
        "point": r.point,
        "severity": r.severity,
        "reason": r.reason,
        "recommended_action": r.recommended_action,
        "status": r.status,
        "priority_score": r.priority_score,
        "aqi_for_ranking": r.aqi_for_ranking,
        "created_at": _iso(r.created_at),
    }


def _source_to_dict(r: models.PollutionSourceEstimate, point_name: Optional[str] = None) -> dict:
    return {
        "point": point_name,
        "generated_at": _iso(r.generated_at),
        "sources": {
            "traffic_pct": r.traffic_pct,
            "industrial_pct": r.industrial_pct,
            "construction_pct": r.construction_pct,
            "waste_burning_pct": r.waste_burning_pct,
            "background_pct": r.background_pct,
        },
        "raw_signals": {
            "congestion_ratio": r.congestion_ratio,
            "wind_speed_mps": r.wind_speed,
            "wind_deg": r.wind_deg,
            "is_stubble_burning_season": r.is_stubble_burning_season,
        },
    }
