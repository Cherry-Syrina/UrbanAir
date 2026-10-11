"""
Live collector sync: CSV logs -> Supabase.

The collectors in aqi_model/ write CSVs; this script mirrors the newest
rows into Supabase so the deployed backend can read persisted data
without depending on the collectors sharing a filesystem.

It is:
  * latest-only (demo mode): each station's AQI/weather row is UPSERTED,
    so the table holds the current reading per station, not a growing log,
  * idempotent and duplicate-safe (one row per station + source),
  * safe to run on a schedule (GitHub Actions / Task Scheduler).

Usage (from urbanair_backend/, DATABASE_URL set):
    python -m scripts.sync_live_to_db
    python -m scripts.sync_live_to_db --csv-dir ..\\urbanair_backend

Scheduling after deployment: see DEPLOYMENT.md ("Keeping data fresh").
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, repository  # noqa: E402

POLLUTANT_COLS = ["co", "no2", "o3", "pm10", "pm25", "so2"]


def _read(path: Path, parse_col: str):
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, parse_dates=[parse_col])
    except (pd.errors.EmptyDataError, ValueError):
        return pd.DataFrame()
    return df


def _aware(dt):
    ts = pd.to_datetime(dt, errors="coerce")
    if pd.isna(ts):
        return None
    py = ts.to_pydatetime()
    return py if py.tzinfo is not None else py.replace(tzinfo=timezone.utc)


def _num(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def sync_once(csv_dir: Path) -> dict:
    """Mirror one round of live CSVs into the DB. Returns per-table counts."""
    if not database.is_configured():
        return {"configured": False}
    if not database.configure(null_pool=True):
        return {"configured": False, "error": "engine"}
    database.create_all()

    csv_dir = Path(csv_dir)
    counts = {"aqi_waqi": 0, "aqi_pollutants": 0, "weather": 0}

    session = database.get_session()
    try:
        # --- Ground-station AQI (WAQI) ---
        # Latest-only: one row per (station, source), updated in place.
        aqi_df = _read(csv_dir / "live_ground_aqi_log.csv", "timestamp")
        for _, r in aqi_df.iterrows():
            name = str(r.get("location_name", "")).strip()
            if not name:
                continue
            lat, lon = _num(r.get("location_lat")), _num(r.get("location_lon"))
            if lat is None or lon is None:
                continue
            sid = repository.ensure_station(session, name, lat, lon, city=name)
            repository.upsert_aqi_reading(session, sid, "waqi", {
                "observed_at": _aware(r.get("timestamp")),
                "aqi": _num(r.get("aqi")),
                "aqi_category": (str(r.get("aqi_category")) if pd.notna(r.get("aqi_category")) else None),
                "external_station_name": (str(r.get("station_name")) if pd.notna(r.get("station_name")) else None),
            })
            counts["aqi_waqi"] += 1

        # --- OWM pollutant observations ---
        poll_df = _read(csv_dir / "live_pollutants_log.csv", "timestamp")
        for _, r in poll_df.iterrows():
            name = str(r.get("location_name", "")).strip()
            if not name:
                continue
            lat, lon = _num(r.get("location_lat")), _num(r.get("location_lon"))
            if lat is None or lon is None:
                continue
            sid = repository.ensure_station(session, name, lat, lon, city=name)
            values = {
                "observed_at": _aware(r.get("timestamp")),
                "aqi": None,
                "aqi_category": None,
            }
            for col in POLLUTANT_COLS:
                values[col] = _num(r.get(col))
            repository.upsert_aqi_reading(session, sid, "owm_nowcast", values)
            counts["aqi_pollutants"] += 1

        # --- Weather ---
        wx_df = _read(csv_dir / "weather_current_log.csv", "fetch_timestamp")
        for _, r in wx_df.iterrows():
            city = str(r.get("city", "")).strip()
            if not city:
                continue
            lat, lon = _num(r.get("lat")), _num(r.get("lon"))
            sid = repository.ensure_station(session, city, lat, lon, city=city) if (lat is not None and lon is not None) else None
            repository.upsert_weather_reading(session, sid, city, "owm", {
                "observed_at": _aware(r.get("fetch_timestamp")),
                "temp": _num(r.get("temp")),
                "feels_like": _num(r.get("feels_like")),
                "humidity": _num(r.get("humidity")),
                "pressure": _num(r.get("pressure")),
                "wind_speed": _num(r.get("wind_speed")),
                "wind_deg": _num(r.get("wind_deg")),
                "wind_gust": _num(r.get("wind_gust")),
                "clouds_pct": _num(r.get("clouds_pct")),
                "visibility": _num(r.get("visibility")),
                "weather_main": (str(r.get("weather_main")) if pd.notna(r.get("weather_main")) else None),
                "weather_desc": (str(r.get("weather_desc")) if pd.notna(r.get("weather_desc")) else None),
            })
            counts["weather"] += 1
        session.commit()
        counts["configured"] = True
    except Exception as exc:
        session.rollback()
        counts["error"] = str(exc)[:200]
    finally:
        session.close()

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync live collector CSVs into Supabase.")
    parser.add_argument("--csv-dir", default=str(BACKEND_DIR),
                        help="Directory containing the live *_log.csv files.")
    args = parser.parse_args()

    if not database.get_database_url():
        print("ERROR: DATABASE_URL (or SUPABASE_DB_URL) is not set.")
        return 1

    result = sync_once(Path(args.csv_dir))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if "error" in result:
        print(f"[{ts}] sync error: {result['error']}")
        return 1
    print(f"[{ts}] synced -> aqi(waqi)={result.get('aqi_waqi', 0)} "
          f"aqi(pollutants)={result.get('aqi_pollutants', 0)} "
          f"weather={result.get('weather', 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
