"""
Database-layer tests. Run fully offline against in-memory SQLite so they
exercise the real ORM/repository/service code paths without needing
Supabase credentials.

NOTE: These are MOCKED/offline tests. They validate SQL, dedup, and query
logic — not a live Supabase connection (see DEPLOYMENT.md for that).
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, repository, service  # noqa: E402


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


class DBTestCase(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        database.reset()
        self.assertTrue(database.configure("sqlite://", force=True))
        database.create_all()
        service._schema_ready = True  # tables already created for this test

    def tearDown(self):
        database.reset()
        service._schema_ready = False


class TestStationsAndReadings(DBTestCase):
    def test_station_dedup_and_reading_dedup(self):
        session = database.get_session()
        sid1 = repository.ensure_station(session, "Delhi", 28.7041, 77.1025)
        sid2 = repository.ensure_station(session, "Delhi", 28.7041, 77.1025)
        self.assertEqual(sid1, sid2)

        rows = [{
            "station_id": sid1,
            "observed_at": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc),
            "aqi": 221.6, "aqi_category": "Poor",
            "co": 0.5, "no2": 24.9, "o3": 18.0,
            "pm10": 108.2, "pm25": 75.1, "so2": 4.3,
            "source": "processed_dataset",
        }]
        first = repository.save_aqi_readings(session, rows)
        session.commit()
        second = repository.save_aqi_readings(session, rows)
        session.commit()
        session.close()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)

    def test_query_history_pagination_and_total(self):
        session = database.get_session()
        sid = repository.ensure_station(session, "Delhi", 28.7041, 77.1025)
        rows = [{
            "station_id": sid,
            "observed_at": datetime(2024, 1, 1, h, 0, tzinfo=timezone.utc),
            "aqi": 100.0 + h,
            "source": "processed_dataset",
        } for h in range(5)]
        repository.save_aqi_readings(session, rows)
        session.commit()
        session.close()

        data = service.query_aqi_history("Delhi", hours=10 ** 7, limit=2, offset=0)
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["items"]), 2)
        self.assertGreaterEqual(data["items"][0]["aqi"], data["items"][1]["aqi"])


class TestForecastsSourcesAlerts(DBTestCase):
    def test_forecast_persistence_and_retrieval(self):
        payload = {
            "city": "Delhi",
            "reference_time": _iso_now(),
            "forecasts": [
                {"horizon_hours": 24, "predicted_aqi": 210.0, "predicted_category": "Poor"},
                {"horizon_hours": 48, "predicted_aqi": 230.0, "predicted_category": "Poor"},
                {"horizon_hours": 72, "predicted_aqi": 250.0, "predicted_category": "Poor"},
            ],
        }
        self.assertEqual(service.persist_forecast("Delhi", payload), 3)
        self.assertEqual(service.persist_forecast("Delhi", payload), 0)  # dedup
        items = service.query_latest_forecasts("Delhi")
        self.assertEqual(len(items), 3)

    def test_source_attribution_persistence(self):
        results = [{
            "point": "Delhi",
            "reference_time": _iso_now(),
            "sources": {"traffic_pct": 30.0, "industrial_pct": 20.0, "construction_pct": 10.0,
                        "waste_burning_pct": 5.0, "background_pct": 35.0},
            "raw_signals": {"congestion_ratio": 0.6, "wind_speed_mps": 2.0,
                            "wind_deg": 300, "is_stubble_burning_season": False},
        }]
        self.assertEqual(service.persist_source_attribution(results), 1)
        self.assertEqual(service.persist_source_attribution(results), 0)  # same hour dedup

    def test_enforcement_persistence_and_query(self):
        priorities = [{
            "point": "Delhi", "priority_score": 88.0, "aqi_for_ranking": 260.0,
            "reasons": ["AQI currently at 260 (Poor)"],
            "recommended_actions": ["Deploy traffic police"],
        }]
        self.assertEqual(service.persist_enforcement(priorities), 1)
        self.assertEqual(service.persist_enforcement(priorities), 0)
        alerts = service.query_alerts(limit=10)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["point"], "Delhi")
        self.assertEqual(alerts[0]["status"], "open")


class TestHealth(DBTestCase):
    def test_health_connected(self):
        h = database.health()
        self.assertTrue(h["configured"])
        self.assertTrue(h["connected"])
        self.assertEqual(h["dialect"], "sqlite")
