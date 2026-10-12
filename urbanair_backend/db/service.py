"""
Service layer: high-level, fail-soft persistence + queries used by the API.

Every function here:
  * checks whether a database is configured first;
  * opens + closes its own session;
  * never raises into the request path — a DB problem is logged and the
    caller continues with its existing (CSV-based) behaviour.

This is what keeps the application fully functional when Supabase is not
configured or temporarily unreachable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import database, repository

logger = logging.getLogger("urbanair.db.service")

# Demo retention. AQI/weather are latest-only snapshots (upserted in
# scripts/sync_live_to_db), so only the generated logs below need trimming:
# forecasts are kept for the latest 7 days, and the hourly source-attribution
# / enforcement logs are capped to the same window.
FORECAST_RETENTION_DAYS = 7
LOG_RETENTION_DAYS = 7


def _as_aware(dt) -> Optional[datetime]:
    """Attach UTC to naive datetimes so they persist consistently."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(dt, datetime) and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _known_points() -> dict:
    """Coordinates of every monitored point (lazy import to avoid cycles)."""
    try:
        import source_attribution as sa

        return dict(sa.CITY_COORDS)
    except Exception:  # pragma: no cover
        return {}


def is_available() -> bool:
    return database.is_configured()


_schema_ready = False


def _ensure_schema() -> None:
    """Create missing tables once per process (idempotent, fail-soft)."""
    global _schema_ready
    if _schema_ready or not database.is_configured():
        return
    try:
        database.create_all()
        _schema_ready = True
    except Exception as exc:  # pragma: no cover
        logger.warning("Schema auto-init skipped: %s", str(exc)[:160])


def db_health() -> dict:
    return database.health()


def init_schema() -> dict:
    """Create missing tables. Safe/idempotent; never drops data."""
    if not database.is_configured():
        return {"ok": False, "message": "Database not configured."}
    try:
        database.create_all()
        return {"ok": True, "message": "Schema ensured (create-if-not-exists)."}
    except Exception as exc:
        logger.error("Schema init failed: %s", str(exc)[:200])
        return {"ok": False, "message": str(exc)[:200]}


def ensure_known_stations() -> int:
    """Populate the stations table from source_attribution.CITY_COORDS."""
    if not database.is_configured():
        return 0
    points = _known_points()
    if not points:
        return 0
    try:
        with session_scope() as session:
            mapping = repository.ensure_stations(session, points)
            return len(mapping)
    except Exception as exc:
        logger.warning("ensure_known_stations skipped: %s", str(exc)[:160])
        return 0


# --------------------------------------------------------------------------- #
# Session helper
# --------------------------------------------------------------------------- #
class session_scope:
    """Context manager yielding a session, committing on success."""

    def __enter__(self):
        self.session = database.get_session()
        return self.session

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
        return False


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def persist_forecast(city: str, payload: dict, model_id: str = "forecast_lgbm") -> int:
    if not database.is_configured() or not payload:
        return 0
    _ensure_schema()
    reference_time = _as_aware(payload.get("reference_time"))
    forecasts = payload.get("forecasts") or []
    if reference_time is None or not forecasts:
        return 0
    try:
        with session_scope() as session:
            station_id = repository.ensure_station(
                session, city, *_coord(city), station_type="city"
            )
            rows = []
            for f in forecasts:
                horizon = int(f.get("horizon_hours"))
                rows.append({
                    "station_id": station_id,
                    "generated_at": _as_aware(datetime.now(timezone.utc)),
                    "target_at": reference_time + timedelta(hours=horizon),
                    "horizon_hours": horizon,
                    "predicted_aqi": float(f.get("predicted_aqi")),
                    "predicted_category": f.get("predicted_category"),
                    "model_id": model_id,
                })
            inserted = repository.save_forecasts(session, rows)
            repository.prune_forecasts(session, FORECAST_RETENTION_DAYS)
            return inserted
    except Exception as exc:
        logger.warning("persist_forecast failed for %s: %s", city, str(exc)[:160])
        return 0


def persist_source_attribution(results: list) -> int:
    if not database.is_configured() or not results:
        return 0
    _ensure_schema()
    inserted = 0
    try:
        with session_scope() as session:
            rows = []
            for r in results:
                point = r.get("point")
                coords = _coord(point)
                if coords is None:
                    continue
                station_id = repository.ensure_station(session, point, *coords)
                generated_at = _as_aware(r.get("reference_time")) or datetime.now(timezone.utc)
                sources = r.get("sources") or {}
                raw = r.get("raw_signals") or {}
                rows.append({
                    "station_id": station_id,
                    "generated_at": _floor_hour(generated_at),
                    "traffic_pct": sources.get("traffic_pct"),
                    "industrial_pct": sources.get("industrial_pct"),
                    "construction_pct": sources.get("construction_pct"),
                    "waste_burning_pct": sources.get("waste_burning_pct"),
                    "background_pct": sources.get("background_pct"),
                    "congestion_ratio": raw.get("congestion_ratio"),
                    "wind_speed": raw.get("wind_speed_mps"),
                    "wind_deg": raw.get("wind_deg"),
                    "is_stubble_burning_season": raw.get("is_stubble_burning_season"),
                })
            inserted = repository.save_source_estimates(session, rows)
            repository.prune_source_estimates(session, LOG_RETENTION_DAYS)
    except Exception as exc:
        logger.warning("persist_source_attribution failed: %s", str(exc)[:160])
    return inserted


def _severity_from_aqi(aqi: Optional[float]) -> str:
    if aqi is None:
        return "Moderate"
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Satisfactory"
    if aqi <= 200:
        return "Moderate"
    if aqi <= 300:
        return "Poor"
    if aqi <= 400:
        return "Very Poor"
    return "Severe"


def persist_enforcement(priorities: list) -> int:
    if not database.is_configured() or not priorities:
        return 0
    _ensure_schema()
    now = datetime.now(timezone.utc)
    hour = _floor_hour(now)
    inserted = 0
    try:
        with session_scope() as session:
            rows = []
            for p in priorities:
                point = p.get("point")
                coords = _coord(point)
                if coords is None:
                    continue
                station_id = repository.ensure_station(session, point, *coords)
                reason = "; ".join(p.get("reasons") or []) or "Routine monitoring"
                action = "; ".join(p.get("recommended_actions") or [])
                severity = _severity_from_aqi(p.get("aqi_for_ranking"))
                dedup_key = f"{point}|{hour.isoformat()}|{severity}|{reason[:120]}"
                rows.append({
                    "station_id": station_id,
                    "point": point,
                    "severity": severity,
                    "reason": reason,
                    "recommended_action": action,
                    "status": "open",
                    "priority_score": p.get("priority_score"),
                    "aqi_for_ranking": p.get("aqi_for_ranking"),
                    "dedup_key": dedup_key,
                    "created_at": now,
                    "updated_at": now,
                })
            inserted = repository.save_enforcement_alerts(session, rows)
            repository.prune_alerts(session, LOG_RETENTION_DAYS)
    except Exception as exc:
        logger.warning("persist_enforcement failed: %s", str(exc)[:160])
    return inserted


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def query_aqi_history(city: str, hours: int = 168, limit: int = 200, offset: int = 0) -> Optional[dict]:
    """Returns a paginated dict, or None if the DB isn't available."""
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            items = repository.get_aqi_history(session, city, hours, limit, offset)
            total = repository.count_aqi_history(session, city, hours)
        return {"city": city, "hours": hours, "limit": limit, "offset": offset,
                "count": len(items), "total": total, "items": items}
    except Exception as exc:
        logger.warning("query_aqi_history failed for %s: %s", city, str(exc)[:160])
        return None


def query_latest_forecasts(city: str) -> Optional[list]:
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            return repository.get_latest_forecasts(session, city)
    except Exception as exc:
        logger.warning("query_latest_forecasts failed for %s: %s", city, str(exc)[:160])
        return None


def query_alerts(status: Optional[str] = None, limit: int = 50, offset: int = 0) -> Optional[list]:
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            return repository.get_alerts(session, status, limit, offset)
    except Exception as exc:
        logger.warning("query_alerts failed: %s", str(exc)[:160])
        return None


def query_latest_reading(city: str) -> Optional[dict]:
    """Most recent stored observation for a city, or None (unavailable/no rows)."""
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            return repository.get_latest_reading(session, city)
    except Exception as exc:
        logger.warning("query_latest_reading failed for %s: %s", city, str(exc)[:160])
        return None


def query_latest_pollutant_reading(city: str) -> Optional[dict]:
    """Most recent stored observation for a city that has any genuine pollutant
    concentration -- which can differ from the latest AQI-only reading.
    Returns None when the database is unavailable or no such row exists."""
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            return repository.get_latest_pollutant_reading(session, city)
    except Exception as exc:
        logger.warning("query_latest_pollutant_reading failed for %s: %s", city, str(exc)[:160])
        return None


def query_recent_readings(city: str, limit: int = 168) -> Optional[list]:
    """Latest stored observations for a city (no time window), or None."""
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            return repository.get_recent_readings(session, city, limit)
    except Exception as exc:
        logger.warning("query_recent_readings failed for %s: %s", city, str(exc)[:160])
        return None


def query_latest_source_estimates() -> Optional[list]:
    """Most recent stored source-attribution snapshot per station, or None."""
    if not database.is_configured():
        return None
    _ensure_schema()
    try:
        with session_scope() as session:
            return repository.get_latest_source_estimates(session)
    except Exception as exc:
        logger.warning("query_latest_source_estimates failed: %s", str(exc)[:160])
        return None


def _coord(name: str):
    return _known_points().get(name)
