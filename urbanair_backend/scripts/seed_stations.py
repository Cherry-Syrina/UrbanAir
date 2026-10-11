"""
Seed the stations table (idempotent, additive, NEVER deletes).

Known points come from ``source_attribution.CITY_COORDS``. Re-running is safe:
stations are matched by their unique ``name`` and reused; existing stations and
their readings are never modified or removed. No coordinates are invented --
only the coordinates already defined in source_attribution.py are used.

Usage (from urbanair_backend/, with DATABASE_URL set):
    python -m scripts.seed_stations                      # seed ALL known points
    python -m scripts.seed_stations --list               # just print known points
    python -m scripts.seed_stations --dry-run            # show plan, write nothing
    python -m scripts.seed_stations --only "Delhi,Noida"
    python -m scripts.seed_stations --type hotspot

This script does NOT import historical readings; use scripts/import_historical
for that. It only ensures the stations referenced by imported data exist.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, repository, service  # noqa: E402
import source_attribution as sa  # noqa: E402


def known_points() -> dict:
    return dict(sa.CITY_COORDS)


def resolve_points(only: str | None) -> dict:
    all_pts = known_points()
    if not only:
        return all_pts
    wanted = [p.strip() for p in only.split(",") if p.strip()]
    selected: dict = {}
    unknown: list = []
    for name in wanted:
        if name in all_pts:
            selected[name] = all_pts[name]
        else:
            unknown.append(name)
    if unknown:
        raise SystemExit(
            "Unknown point(s): " + ", ".join(unknown)
            + "\nKnown points: " + ", ".join(all_pts)
        )
    return selected


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Seed the stations table (idempotent).")
    parser.add_argument("--list", action="store_true", help="List known points and exit.")
    parser.add_argument("--only", default=None, help="Comma-separated subset of point names.")
    parser.add_argument("--type", default="city", help="station_type to store (default: city).")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan; write nothing.")
    args = parser.parse_args(argv)

    pts = known_points()

    if args.list:
        print(f"{len(pts)} known points (source_attribution.CITY_COORDS):")
        for name, (lat, lon) in pts.items():
            print(f"  {name:24s} {lat:8.4f}, {lon:9.4f}")
        return 0

    try:
        selected = resolve_points(args.only)
    except SystemExit as exc:
        print(str(exc))
        return 2

    print(f"Selected {len(selected)} of {len(pts)} known points (station_type={args.type}).")
    for name in selected:
        print(f"  + {name}")

    if args.dry_run:
        print("DRY RUN: nothing written.")
        return 0

    if not database.get_database_url():
        print("ERROR: DATABASE_URL (or SUPABASE_DB_URL) is not set.")
        print("Set it in your environment or urbanair_backend/.env first.")
        return 1

    if not database.configure():
        print("ERROR: could not create a database engine from DATABASE_URL.")
        return 1

    result = service.init_schema()
    if not result["ok"]:
        print(f"ERROR: {result['message']}")
        return 1

    try:
        with service.session_scope() as session:
            mapping = repository.ensure_stations(session, selected, station_type=args.type)
    except Exception as exc:
        print(f"ERROR seeding stations: {str(exc)[:200]}")
        return 1

    print(f"OK: {len(mapping)} station(s) seeded/verified (idempotent; nothing deleted).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
