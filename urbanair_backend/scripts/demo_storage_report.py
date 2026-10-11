"""
Dry-run report for the demo's latest-only storage. READ-ONLY.

Shows, without changing anything:
  * current row counts per table;
  * how many aqi/weather rows a sync would collapse (duplicates that exist
    today only if data was written before latest-only mode);
  * how many forecast / source-attribution / enforcement rows are older than
    the 7-day retention window and would be pruned on the next write.

Run (from urbanair_backend/, DATABASE_URL set):
    python -m scripts.demo_storage_report

This script never inserts, updates, or deletes. It is safe to run against
the production database.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, models  # noqa: E402

RETENTION_DAYS = 7

TABLES = {
    "stations": models.Station,
    "aqi_readings": models.AqiReading,
    "weather_readings": models.WeatherReading,
    "aqi_forecasts": models.AqiForecast,
    "pollution_source_estimates": models.PollutionSourceEstimate,
    "enforcement_alerts": models.EnforcementAlert,
}


def main() -> int:
    if not database.get_database_url():
        print("ERROR: DATABASE_URL (or SUPABASE_DB_URL) is not set.")
        return 1
    if not database.configure(null_pool=True):
        print("ERROR: could not create a database engine from DATABASE_URL.")
        return 1

    session = database.get_session()
    try:
        def count(model):
            return int(session.execute(select(func.count()).select_from(model)).scalar() or 0)

        counts = {name: count(model) for name, model in TABLES.items()}
        print("Table row counts:")
        for name, n in counts.items():
            print(f"  {name:30s} {n}")

        # Duplicate rows that latest-only upserts would collapse.
        aqi_distinct = int(session.execute(
            select(func.count()).select_from(
                select(models.AqiReading.station_id, models.AqiReading.source)
                .distinct().subquery()
            )
        ).scalar() or 0)
        wx_distinct = int(session.execute(
            select(func.count()).select_from(
                select(models.WeatherReading.station_id, models.WeatherReading.source)
                .distinct().subquery()
            )
        ).scalar() or 0)
        aqi_collapse = counts["aqi_readings"] - aqi_distinct
        wx_collapse = counts["weather_readings"] - wx_distinct

        cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

        def old_count(model, column):
            return int(session.execute(
                select(func.count()).select_from(model).where(column < cutoff)
            ).scalar() or 0)

        fc_old = old_count(models.AqiForecast, models.AqiForecast.generated_at)
        src_old = old_count(models.PollutionSourceEstimate, models.PollutionSourceEstimate.generated_at)
        al_old = old_count(models.EnforcementAlert, models.EnforcementAlert.created_at)

        print("\nLatest-only effect (rows a sync/write would collapse):")
        print(f"  aqi_readings duplicate rows     : {aqi_collapse}")
        print(f"  weather_readings duplicate rows : {wx_collapse}")

        print(f"\nRetention (older than {RETENTION_DAYS} days -> would be pruned):")
        print(f"  aqi_forecasts                   : {fc_old}")
        print(f"  pollution_source_estimates      : {src_old}")
        print(f"  enforcement_alerts              : {al_old}")

        destructive = aqi_collapse + wx_collapse + fc_old + src_old + al_old
        print("\n----- verdict -----")
        if destructive == 0:
            print("DRY RUN: nothing to delete. Tables are already latest-only / within retention.")
        else:
            print(f"DRY RUN: {destructive} row(s) would be removed on the next sync/prune.")
        print("No changes were made by this report.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
