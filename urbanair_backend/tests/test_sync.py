"""
Live-sync test: verifies scripts/sync_live_to_db.py mirrors the collector
CSVs into the database and is duplicate-safe. Offline (SQLite).
"""

import csv
import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, service  # noqa: E402
from db import models, repository  # noqa: E402
from scripts import sync_live_to_db  # noqa: E402


def _count(model):
    from sqlalchemy import func, select
    session = database.get_session()
    try:
        return session.execute(select(func.count()).select_from(model)).scalar() or 0
    finally:
        session.close()


class TestLiveSync(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        database.reset()
        database.configure("sqlite://", force=True)
        database.create_all()
        service._schema_ready = True

    def tearDown(self):
        database.reset()
        service._schema_ready = False

    def _write(self, directory, name, header, rows):
        with open(Path(directory) / name, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)

    def test_sync_once_is_latest_only(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "live_ground_aqi_log.csv",
                        ["timestamp", "location_name", "location_lat", "location_lon",
                         "aqi", "aqi_category", "station_name"],
                        [["2024-01-01 00:00:00", "Delhi", 28.7041, 77.1025, 150, "Moderate", "Test stn"]])
            self._write(d, "live_pollutants_log.csv",
                        ["timestamp", "location_name", "location_lat", "location_lon",
                         "co", "no2", "o3", "pm10", "pm25", "so2", "owm_aqi_index"],
                        [["2024-01-01 00:00:00", "Delhi", 28.7041, 77.1025, 0.5, 20, 30, 100, 70, 5, 3]])
            self._write(d, "weather_current_log.csv",
                        ["fetch_timestamp", "city", "lat", "lon", "temp", "feels_like",
                         "humidity", "pressure", "wind_speed", "wind_deg", "wind_gust",
                         "clouds_pct", "visibility", "weather_main", "weather_desc"],
                        [["2024-01-01T00:00:00+00:00", "Delhi", 28.7041, 77.1025, 25, 26, 40,
                          1000, 2.0, 270, 3.0, 10, 10000, "Clear", "clear sky"]])

            first = sync_live_to_db.sync_once(Path(d))
            self.assertTrue(first["configured"])
            self.assertEqual(first["aqi_waqi"], 1)
            self.assertEqual(first["aqi_pollutants"], 1)
            self.assertEqual(first["weather"], 1)
            # one row per (station, source) for AQI; one weather row
            self.assertEqual(_count(models.AqiReading), 2)
            self.assertEqual(_count(models.WeatherReading), 1)

            # Re-run with a NEWER reading: it must UPDATE in place, not append.
            self._write(d, "live_ground_aqi_log.csv",
                        ["timestamp", "location_name", "location_lat", "location_lon",
                         "aqi", "aqi_category", "station_name"],
                        [["2024-06-01 12:00:00", "Delhi", 28.7041, 77.1025, 310, "Very Poor", "Test stn"]])
            second = sync_live_to_db.sync_once(Path(d))
            self.assertEqual(second["aqi_waqi"], 1)

            self.assertEqual(_count(models.AqiReading), 2)   # still one per source
            session = database.get_session()
            try:
                latest = repository.get_latest_reading(session, "Delhi")
            finally:
                session.close()
            self.assertEqual(latest["aqi"], 310.0)

