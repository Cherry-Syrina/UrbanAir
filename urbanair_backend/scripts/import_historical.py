"""
Historical data migration: processed_aqi_data.csv -> Supabase (aqi_readings).

The dataset (5 cities, hourly, 2019-01-01 .. 2024-01-01) contains:
    timestamp, location_name, location_lat, location_lon,
    co, no2, o3, pm10, pm25, so2, aqi, aqi_category, ...

Behavior / safety:
  * Reads in chunks, validates each row, skips malformed values.
  * Duplicate-safe: re-running imports nothing already present
    (unique on station_id + observed_at + source).
  * NON-DESTRUCTIVE: only INSERTs; never updates or deletes.
  * Reports imported / skipped / invalid counts.
  * Original CSV is never modified.

Usage (from urbanair_backend/, DATABASE_URL set):
    python -m scripts.import_historical --csv ..\\aqi_model\\processed_aqi_data.csv
    python -m scripts.import_historical --dry-run
    python -m scripts.import_historical --limit 5000
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

DEFAULT_CSV = BACKEND_DIR.parent / "aqi_model" / "processed_aqi_data.csv"
SOURCE = "processed_dataset"

POLLUTANT_COLS = ["co", "no2", "o3", "pm10", "pm25", "so2"]


def _as_aware(dt) -> datetime:
    py = pd.to_datetime(dt).to_pydatetime()
    if py.tzinfo is None:
        py = py.replace(tzinfo=timezone.utc)
    return py


def _num(value):
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_row(row) -> bool:
    if pd.isna(row.get("timestamp")) or pd.isna(row.get("location_name")):
        return False
    return _num(row.get("aqi")) is not None


def run_import(csv_path: Path, batch_size: int = 5000, limit: int = 0,
               dry_run: bool = False, create_tables: bool = True) -> dict:
    """Import the historical CSV. Returns a counts dict. Non-destructive."""
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    if not dry_run:
        if database.get_engine() is None and not database.configure():
            raise RuntimeError("Database is not configured (set DATABASE_URL).")
        if create_tables:
            database.create_all()

    imported = skipped = invalid = 0
    station_cache: dict = {}
    total_valid = 0

    for chunk in pd.read_csv(csv_path, chunksize=batch_size):
        records = []
        for _, r in chunk.iterrows():
            if not _valid_row(r):
                invalid += 1
                continue
            total_valid += 1
            if limit and total_valid > limit:
                break

            name = str(r["location_name"]).strip()
            lat, lon = _num(r.get("location_lat")), _num(r.get("location_lon"))
            if lat is None or lon is None:
                invalid += 1
                continue

            rec = {
                "station_name": name, "lat": lat, "lon": lon,
                "observed_at": _as_aware(r["timestamp"]),
                "aqi": _num(r.get("aqi")),
                "aqi_category": (str(r.get("aqi_category")) if pd.notna(r.get("aqi_category")) else None),
            }
            for col in POLLUTANT_COLS:
                rec[col] = _num(r.get(col))
            records.append(rec)

        if dry_run or not records:
            if limit and total_valid >= limit:
                break
            continue

        session = database.get_session()
        try:
            for rec in records:
                if rec["station_name"] not in station_cache:
                    station_cache[rec["station_name"]] = repository.ensure_station(
                        session, rec["station_name"], rec["lat"], rec["lon"],
                        station_type="city", city=rec["station_name"],
                    )
            insert_rows = [{
                "station_id": station_cache[rec["station_name"]],
                "observed_at": rec["observed_at"],
                "aqi": rec["aqi"], "aqi_category": rec["aqi_category"],
                "co": rec["co"], "no2": rec["no2"], "o3": rec["o3"],
                "pm10": rec["pm10"], "pm25": rec["pm25"], "so2": rec["so2"],
                "source": SOURCE,
            } for rec in records]
            got = repository.save_aqi_readings(session, insert_rows)
            session.commit()
            imported += got
            skipped += len(insert_rows) - got
        except Exception as exc:
            session.rollback()
            print(f"  [warn] batch failed: {str(exc)[:160]}")
        finally:
            session.close()

        if imported and imported % 50000 == 0:
            print(f"  ... {imported} rows inserted so far")

        if limit and total_valid >= limit:
            break

    return {"valid": total_valid, "invalid": invalid,
            "imported": imported, "skipped": skipped, "dry_run": dry_run}


def main() -> int:
    parser = argparse.ArgumentParser(description="Import historical AQI CSV into Supabase.")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="Path to processed_aqi_data.csv")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--limit", type=int, default=0, help="Max valid rows to import (0 = all).")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, no DB writes.")
    args = parser.parse_args()

    if not args.dry_run and not database.get_database_url():
        print("ERROR: DATABASE_URL (or SUPABASE_DB_URL) is not set.")
        return 1

    print(f"Reading {args.csv} ...")
    result = run_import(Path(args.csv), batch_size=args.batch_size,
                        limit=args.limit, dry_run=args.dry_run)

    print("\n----- summary -----")
    print(f"valid rows seen   : {result['valid']}")
    print(f"invalid rows      : {result['invalid']}")
    if result["dry_run"]:
        print("dry-run: no rows written")
    else:
        print(f"inserted          : {result['imported']}")
        print(f"skipped (dupes)   : {result['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
