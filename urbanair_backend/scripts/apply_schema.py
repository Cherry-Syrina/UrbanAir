"""
Apply the database schema (create tables/indexes if they do not exist).

This is SAFE and IDEMPOTENT: it never drops a table or deletes data.
Prefer applying database/schema.sql in the Supabase SQL Editor for a
reviewable, version-controlled migration; this script is a convenience
for local/dev use.

Usage (from urbanair_backend/, with DATABASE_URL set):
    python -m scripts.apply_schema
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database  # noqa: E402
from db import service  # noqa: E402


def main() -> int:
    if not database.get_database_url():
        print("ERROR: DATABASE_URL (or SUPABASE_DB_URL) is not set.")
        print("Set it in your environment or urbanair_backend/.env first.")
        return 1

    if not database.configure(null_pool=True):
        print("ERROR: could not create a database engine from DATABASE_URL.")
        return 1

    result = service.init_schema()
    print(result["message"])
    if not result["ok"]:
        return 1

    count = service.ensure_known_stations()
    print(f"stations seeded/verified: {count}")
    print("Schema is up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
