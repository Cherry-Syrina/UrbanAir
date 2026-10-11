"""
Tests for the Supabase-backed fallback used when the live collector CSVs are
NOT present in the running container (e.g. the deployed Render web service).

Offline: in-memory SQLite is used and live sources are patched out, so these
prove the *fallback* wiring without touching Supabase or real data.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, repository, service  # noqa: E402


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def _empty_live():
    """An empty live-history frame that still has the expected columns, so the
    endpoints' ``df[df["location_name"] == city]`` slicing does not KeyError."""
    return pd.DataFrame(columns=["timestamp", "location_name", "aqi", "pm25", "pm10"])


class FallbackTestCase(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        database.reset()
        self.assertTrue(database.configure("sqlite://", force=True))
        database.create_all()
        service._schema_ready = True
        self._seed()

    def tearDown(self):
        database.reset()
        service._schema_ready = False

    def _seed(self):
        session = database.get_session()
        sid = repository.ensure_station(session, "Delhi", 28.7041, 77.1025)
        repository.save_aqi_readings(session, [
            {"station_id": sid,
             "observed_at": datetime(2024, 1, 1, h, 0, tzinfo=timezone.utc),
             "aqi": 200.0 + h, "aqi_category": "Poor",
             "co": 0.5, "no2": 24.0, "o3": 18.0,
             "pm10": 108.0, "pm25": 75.0, "so2": 4.0,
             "source": "processed_dataset"}
            for h in range(3)
        ])
        session.commit()
        session.close()

        service.persist_forecast("Delhi", {
            "city": "Delhi",
            "reference_time": _iso_now(),
            "forecasts": [
                {"horizon_hours": 24, "predicted_aqi": 210.0, "predicted_category": "Poor"},
                {"horizon_hours": 48, "predicted_aqi": 230.0, "predicted_category": "Poor"},
            ],
        })
        service.persist_source_attribution([{
            "point": "Delhi",
            "reference_time": _iso_now(),
            "sources": {"traffic_pct": 30.0, "industrial_pct": 20.0, "construction_pct": 10.0,
                        "waste_burning_pct": 5.0, "background_pct": 35.0},
            "raw_signals": {"congestion_ratio": 0.6, "wind_speed_mps": 2.0,
                            "wind_deg": 300, "is_stubble_burning_season": False},
        }])
        service.persist_enforcement([{
            "point": "Delhi", "priority_score": 88.0, "aqi_for_ranking": 260.0,
            "reasons": ["AQI currently at 260 (Poor)"],
            "recommended_actions": ["Deploy traffic police"],
        }])


class TestRepositoryFallbackReads(FallbackTestCase):
    def test_latest_reading(self):
        r = service.query_latest_reading("Delhi")
        self.assertIsNotNone(r)
        self.assertEqual(r["aqi"], 202.0)  # newest hour
        self.assertEqual(r["category"], "Poor")

    def test_recent_readings_newest_first(self):
        rows = service.query_recent_readings("Delhi", limit=10)
        self.assertEqual(len(rows), 3)
        self.assertGreaterEqual(rows[0]["aqi"], rows[-1]["aqi"])

    def test_latest_source_estimates(self):
        rows = service.query_latest_source_estimates()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["point"], "Delhi")
        self.assertEqual(rows[0]["sources"]["traffic_pct"], 30.0)

    def test_unconfigured_returns_none(self):
        database.reset()
        service._schema_ready = False
        self.assertIsNone(service.query_latest_reading("Delhi"))
        self.assertIsNone(service.query_recent_readings("Delhi"))
        self.assertIsNone(service.query_latest_source_estimates())


class TestEndpointFallback(FallbackTestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from app import main
        return TestClient(main.app), main

    def test_current_uses_stored_observation(self):
        client, main = self._client()
        with patch.object(main, "_load_latest_live_pollutants", return_value=None), \
                patch.object(main, "_load_latest_ground_aqi", return_value=None):
            resp = client.get("/api/current/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["data_origin"], "stored_observation")
        self.assertTrue(body["is_estimated"])
        self.assertEqual(body["aqi"], 202.0)

    def test_current_503_when_no_stored_reading(self):
        client, main = self._client()
        with patch.object(main, "_load_latest_live_pollutants", return_value=None), \
                patch.object(main, "_load_latest_ground_aqi", return_value=None):
            resp = client.get("/api/current/Faridabad")  # known city, no stored rows
        self.assertEqual(resp.status_code, 503)

    def test_historical_uses_recent_readings(self):
        client, main = self._client()
        with patch.object(main.live_history, "get_live_history",
                          return_value=_empty_live()):
            resp = client.get("/api/historical/Delhi?hours=10")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["data_origin"], "stored_observation")
        # chronological ascending
        self.assertLessEqual(rows[0]["aqi"], rows[-1]["aqi"])

    def test_forecast_uses_stored_forecast(self):
        client, main = self._client()
        with patch.object(main.live_history, "get_live_history",
                          return_value=_empty_live()):
            resp = client.get("/api/forecast/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["data_origin"], "database_stored_forecast")
        self.assertEqual([f["horizon_hours"] for f in body["forecasts"]], [24, 48])

    def test_source_attribution_uses_stored_snapshot(self):
        client, main = self._client()
        with patch.object(main.sa, "run_for_all_points",
                          side_effect=FileNotFoundError("traffic_log.csv")):
            resp = client.get("/api/source-attribution")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["point"], "Delhi")
        self.assertEqual(rows[0]["data_origin"], "stored_snapshot")

    def test_enforcement_uses_stored_alert(self):
        from fastapi import HTTPException

        client, main = self._client()
        # Force the "live attribution unavailable" branch so enforcement serves
        # stored alerts instead of recomputing a live ranking.
        with patch.object(main, "source_attribution_all",
                          side_effect=HTTPException(503, "no live attribution")):
            resp = client.get("/api/enforcement")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["data_origin"], "stored_alert")
        self.assertEqual(rows[0]["point"], "Delhi")
        self.assertEqual(rows[0]["rank"], 1)
