"""
Tests for the demo's latest-only storage (upserts) and 7-day retention.
Offline against in-memory SQLite.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

from sqlalchemy import func, select

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, models, repository, service  # noqa: E402


class DemoStorageTestCase(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        database.reset()
        database.configure("sqlite://", force=True)
        database.create_all()
        service._schema_ready = True
        self.session = database.get_session()

    def tearDown(self):
        self.session.close()
        database.reset()
        service._schema_ready = False

    def _count(self, model):
        session = database.get_session()
        try:
            return int(session.execute(select(func.count()).select_from(model)).scalar() or 0)
        finally:
            session.close()


class TestLatestOnlyUpsert(DemoStorageTestCase):
    def test_aqi_upsert_updates_in_place(self):
        sid = repository.ensure_station(self.session, "Delhi", 28.7041, 77.1025)
        t1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        repository.upsert_aqi_reading(self.session, sid, "waqi", {"observed_at": t1, "aqi": 150.0})
        self.session.commit()
        self.assertEqual(self._count(models.AqiReading), 1)

        t2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
        result = repository.upsert_aqi_reading(self.session, sid, "waqi", {"observed_at": t2, "aqi": 310.0})
        self.session.commit()
        self.assertEqual(result, "updated")
        self.assertEqual(self._count(models.AqiReading), 1)
        self.assertEqual(repository.get_latest_reading(self.session, "Delhi")["aqi"], 310.0)

    def test_aqi_upsert_distinct_sources_coexist(self):
        sid = repository.ensure_station(self.session, "Delhi", 28.7041, 77.1025)
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        repository.upsert_aqi_reading(self.session, sid, "waqi", {"observed_at": t, "aqi": 150.0})
        repository.upsert_aqi_reading(self.session, sid, "owm_nowcast", {"observed_at": t, "pm25": 70.0})
        self.session.commit()
        self.assertEqual(self._count(models.AqiReading), 2)

    def test_weather_upsert_updates_in_place(self):
        sid = repository.ensure_station(self.session, "Delhi", 28.7041, 77.1025)
        repository.upsert_weather_reading(self.session, sid, "Delhi", "owm",
                                          {"observed_at": datetime(2024, 1, 1, tzinfo=timezone.utc), "temp": 20.0})
        repository.upsert_weather_reading(self.session, sid, "Delhi", "owm",
                                          {"observed_at": datetime(2024, 1, 2, tzinfo=timezone.utc), "temp": 35.0})
        self.session.commit()
        self.assertEqual(self._count(models.WeatherReading), 1)
        row = self.session.execute(select(models.WeatherReading)).scalars().one()
        self.assertEqual(row.temp, 35.0)

    def test_upsert_collapses_existing_duplicates(self):
        sid = repository.ensure_station(self.session, "Delhi", 28.7041, 77.1025)
        for h in range(3):  # simulate leftover history rows
            self.session.add(models.AqiReading(
                station_id=sid, source="waqi",
                observed_at=datetime(2024, 1, 1, h, tzinfo=timezone.utc), aqi=100.0 + h))
        self.session.commit()
        self.assertEqual(self._count(models.AqiReading), 3)
        repository.upsert_aqi_reading(self.session, sid, "waqi",
                                      {"observed_at": datetime(2024, 2, 1, tzinfo=timezone.utc), "aqi": 200.0})
        self.session.commit()
        self.assertEqual(self._count(models.AqiReading), 1)


class TestRetention(DemoStorageTestCase):
    def test_prune_forecasts_removes_only_old(self):
        sid = repository.ensure_station(self.session, "Delhi", 28.7041, 77.1025)
        now = datetime.now(timezone.utc)
        self.session.add(models.AqiForecast(
            station_id=sid, generated_at=now - timedelta(days=10),
            target_at=now + timedelta(hours=24), horizon_hours=24, predicted_aqi=200.0))
        self.session.add(models.AqiForecast(
            station_id=sid, generated_at=now - timedelta(hours=1),
            target_at=now + timedelta(hours=48), horizon_hours=48, predicted_aqi=220.0))
        self.session.commit()
        removed = repository.prune_forecasts(self.session, days=7)
        self.session.commit()
        self.assertEqual(removed, 1)
        self.assertEqual(self._count(models.AqiForecast), 1)

    def test_persist_forecast_prunes_old_and_keeps_recent(self):
        sid = repository.ensure_station(self.session, "Delhi", 28.7041, 77.1025)
        now = datetime.now(timezone.utc)
        self.session.add(models.AqiForecast(
            station_id=sid, generated_at=now - timedelta(days=30),
            target_at=now, horizon_hours=24, predicted_aqi=200.0))
        self.session.commit()

        payload = {
            "city": "Delhi",
            "reference_time": now.isoformat(),
            "forecasts": [{"horizon_hours": 24, "predicted_aqi": 250.0, "predicted_category": "Poor"}],
        }
        service.persist_forecast("Delhi", payload)  # commits + prunes internally
        self.assertEqual(self._count(models.AqiForecast), 1)  # old one pruned, new one kept
