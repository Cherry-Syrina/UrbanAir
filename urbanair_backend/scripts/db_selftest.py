"""
Safe database self-test for the Supabase PostgreSQL integration.

It NEVER drops tables or mutates data:
  1. checks DATABASE_URL is set and the engine connects (SELECT 1)
  2. reports whether the 6 expected tables already exist
  3. reads a row count from each existing table
  4. (optional --write-test) inserts one row inside a transaction and then
     ROLLS IT BACK, proving write permission with zero persisted changes.

Usage (from urbanair_backend/, with DATABASE_URL set):
    python -m scripts.db_selftest
    python -m scripts.db_selftest --write-test
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database  # noqa: E402
from db import models  # noqa: E402,F401  (register metadata)

EXPECTED_TABLES = [
    "stations",
    "aqi_readings",
    "weather_readings",
    "aqi_forecasts",
    "pollution_source_estimates",
    "enforcement_alerts",
]


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    write_test = "--write-test" in argv

    if not database.get_database_url():
        print("NOT CONFIGURED: DATABASE_URL / SUPABASE_DB_URL is not set.")
        print("Add the Session Pooler string to urbanair_backend/.env first.")
        return 2

    if not database.configure():
        print("FAIL: could not build a database engine from DATABASE_URL.")
        return 1

    engine = database.get_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: connection error: {str(exc)[:240]}")
        return 1
    print(f"PASS: connected (dialect={engine.dialect.name}, host={engine.url.host})")

    existing = set(inspect(engine).get_table_names())
    missing = [t for t in EXPECTED_TABLES if t not in existing]
    if missing:
        print("SCHEMA: NOT APPLIED -> missing tables: " + ", ".join(missing))
    else:
        print("SCHEMA: APPLIED -> all 6 tables present")

    print("READ test:")
    with engine.connect() as conn:
        for table in EXPECTED_TABLES:
            if table in existing:
                count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
                print(f"  {table:<28} {count} rows")

    if write_test:
        if missing:
            print("WRITE: skipped (schema not fully applied).")
            return 1
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                before = conn.execute(text("SELECT count(*) FROM stations")).scalar()
                conn.execute(
                    text(
                        "INSERT INTO stations (name, station_type, city, lat, lon) "
                        "VALUES (:n, 'selftest', 'selftest', 0, 0)"
                    ),
                    {"n": "__urbanair_selftest__"},
                )
                during = conn.execute(text("SELECT count(*) FROM stations")).scalar()
                print(f"WRITE test: insert ok ({before} -> {during} rows); rolling back...")
            finally:
                trans.rollback()
            after = conn.execute(text("SELECT count(*) FROM stations")).scalar()
        if after == before:
            print("PASS: write permission verified (transaction rolled back, no data kept).")
            return 0
        print(f"WARN: row count changed unexpectedly ({before} -> {after}).")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
