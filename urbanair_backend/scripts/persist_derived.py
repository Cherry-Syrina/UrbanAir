"""
Persist DERIVED artifacts (forecasts + source attribution) into Supabase.

Why this exists
---------------
The deployed web service has no collector CSVs, so ``/api/forecast``,
``/api/health-advisory`` and ``/api/source-attribution`` fall back to the
persisted tables. Only readings were ever synced, so those tables stayed empty
and the endpoints returned 503. This script runs in the SAME GitHub Actions job
that collects the CSVs, so the derived tables are populated from REAL collected
data -- nothing is fabricated, backfilled with placeholders, or suppressed.

What it computes (honestly)
---------------------------
* forecasts          -> needs >= 1 live hourly row per trained city. Uses the
  exact same model + feature builder as ``/api/forecast`` (``forecast_engine``)
  so the stored rows equal what the live endpoint would have returned.
* source attribution -> needs ``traffic_log.csv`` + ``weather_current_log.csv``.

Anything it cannot compute is skipped and reported, never invented.

Usage (from urbanair_backend/, DATABASE_URL set):
    python -m scripts.persist_derived --csv-dir ../aqi_model
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, service as db_service  # noqa: E402


def persist_derived(csv_dir, cities=None) -> dict:
    """Compute + persist forecasts and source attribution from ``csv_dir``.

    Returns a JSON-safe summary dict (never raises for expected data gaps)."""
    from db import database as _database
    import forecast_engine
    import live_history
    import source_attribution as sa

    csv_dir = str(Path(csv_dir).resolve())
    if cities is None:
        cities = list(sa.TRAINED_CITIES)

    result = {
        "csv_dir": csv_dir,
        "configured": False,
        "forecasts_built": 0,
        "forecasts_persisted": 0,
        "cities": [],
        "cities_skipped": [],
        "attribution_points": 0,
        "attribution_persisted": 0,
        "attribution_error": None,
    }

    # live_history / source_attribution read the CSV dir at call time.
    prior_csv_dir = os.environ.get("URBANAIR_LIVE_CSV_DIR")
    os.environ["URBANAIR_LIVE_CSV_DIR"] = csv_dir
    try:
        if not _database.is_configured():
            return result
        if not _database.configure(null_pool=True):
            result["error"] = "engine"
            return result
        _database.create_all()
        result["configured"] = True

        # --- Forecasts: same engine + feature builder the live endpoint uses ---
        live_df = live_history.get_live_history()
        for city in cities:
            city_df = live_df[live_df["location_name"] == city].reset_index(drop=True)
            if city_df.empty or city_df["aqi"].dropna().empty:
                result["cities_skipped"].append(city)
                continue
            payload = forecast_engine.build_forecast_payload(city, city_df)
            result["forecasts_built"] += len(payload.get("forecasts", []))
            result["forecasts_persisted"] += db_service.persist_forecast(city, payload)
            result["cities"].append(city)

        # --- Source attribution: needs traffic_log.csv + weather_current_log.csv ---
        try:
            attribution = sa.run_for_all_points()
            result["attribution_points"] = len(attribution)
            result["attribution_persisted"] = db_service.persist_source_attribution(attribution)
        except FileNotFoundError as exc:
            result["attribution_error"] = f"missing collector CSV: {exc}"
        except Exception as exc:  # never crash the job on an attribution problem
            result["attribution_error"] = str(exc)[:200]

        return result
    finally:
        if prior_csv_dir is None:
            os.environ.pop("URBANAIR_LIVE_CSV_DIR", None)
        else:
            os.environ["URBANAIR_LIVE_CSV_DIR"] = prior_csv_dir


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute + persist forecasts and source attribution into Supabase.")
    parser.add_argument("--csv-dir",
                        default=os.environ.get("URBANAIR_LIVE_CSV_DIR") or str(BACKEND_DIR),
                        help="Directory the collectors wrote their *_log.csv files to.")
    parser.add_argument("--cities", nargs="*", default=None,
                        help="Cities to forecast (default: the 5 trained cities).")
    args = parser.parse_args()

    if not database.is_configured():
        print("ERROR: DATABASE_URL (or SUPABASE_DB_URL) is not set.")
        return 1

    result = persist_derived(args.csv_dir, args.cities)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not result.get("configured"):
        print(f"[{ts}] derived persist skipped: {result.get('error', 'database not configured')}")
        return 1
    print(f"[{ts}] derived -> forecasts built={result['forecasts_built']} "
          f"persisted={result['forecasts_persisted']} "
          f"cities={len(result['cities'])} skipped={result['cities_skipped']} "
          f"| attribution points={result['attribution_points']} "
          f"persisted={result['attribution_persisted']} "
          f"error={result['attribution_error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
