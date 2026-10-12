"""
Tests for the Supabase-backed fallback used when the live collector CSVs are
NOT present in the running container (e.g. the deployed Render web service).

Offline: in-memory SQLite is used and live sources are patched out, so these
prove the *fallback* wiring without touching Supabase or real data.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
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


class TestHealthAdvisoryFallback(FallbackTestCase):
    """Regression tests for the /api/health-advisory 500 (and the CORS error
    the browser saw because the 500 bypassed CORSMiddleware).

    Root cause: stored forecasts carry `data_maturity=None`, and the payload
    builder did `fc["data_maturity"]["is_warming_up"]`."""

    def _client(self):
        from fastapi.testclient import TestClient
        from app import main
        return TestClient(main.app), main

    def _stored_only(self, main):
        # Force the stored-reading + stored-forecast path (no live CSVs).
        return (
            patch.object(main, "_load_latest_live_pollutants", return_value=None),
            patch.object(main, "_load_latest_ground_aqi", return_value=None),
            patch.object(main.live_history, "get_live_history", return_value=_empty_live()),
        )

    def test_stored_forecast_with_none_data_maturity_returns_200(self):
        client, main = self._client()
        p1, p2, p3 = self._stored_only(main)
        with p1, p2, p3:
            resp = client.get("/api/health-advisory/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["current_aqi"], 202.0)
        self.assertEqual(body["current_category"], "Poor")
        self.assertEqual(body["forecast_status"], "stored")
        self.assertIsNone(body["forecast_is_warming_up"])
        self.assertEqual(body["forecast_data_origin"], "database_stored_forecast")
        self.assertEqual(len(body["forecast_advisories"]), 2)

    def test_live_forecast_with_data_maturity(self):
        client, main = self._client()
        live_fc = {
            "city": "Delhi",
            "reference_time": _iso_now(),
            "current_aqi": 210.0,
            "forecasts": [
                {"horizon_hours": 24, "predicted_aqi": 220.0, "predicted_category": "Poor"},
            ],
            "model_metrics": {},
            "data_maturity": {"hours_available": 10, "is_warming_up": True},
            "data_origin": "live_measurement",
        }
        p1, p2, _ = self._stored_only(main)
        with p1, p2, patch.object(main, "forecast", return_value=live_fc):
            resp = client.get("/api/health-advisory/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["forecast_is_warming_up"])
        self.assertEqual(body["forecast_status"], "warming_up")
        self.assertEqual(body["forecast_data_origin"], "live_measurement")
        self.assertEqual(len(body["forecast_advisories"]), 1)

    def test_forecast_unavailable_returns_200_not_500(self):
        from fastapi import HTTPException

        client, main = self._client()
        p1, p2, _ = self._stored_only(main)
        with p1, p2, patch.object(main, "forecast",
                                  side_effect=HTTPException(503, "no forecast")):
            resp = client.get("/api/health-advisory/Delhi")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["forecast_status"], "unavailable")
        self.assertEqual(body["forecast_advisories"], [])
        self.assertIsNone(body["forecast_is_warming_up"])
        # current-AQI advisory is still present and useful
        self.assertEqual(body["current_aqi"], 202.0)
        self.assertTrue(body["general_advisory"])

    def test_build_payload_handles_none_data_maturity_and_none_fc(self):
        from app.main import _build_health_advisory_payload

        current = {"aqi": 180.0, "category": "Moderate"}
        stored_fc = {"forecasts": [
            {"horizon_hours": 24, "predicted_aqi": 190.0, "predicted_category": "Moderate"},
        ], "data_maturity": None, "data_origin": "database_stored_forecast"}

        for fc, expected_status in ((stored_fc, "stored"), (None, "unavailable")):
            payload = _build_health_advisory_payload(current, fc)
            self.assertEqual(payload["current_aqi"], 180.0)
            self.assertEqual(payload["current_category"], "Moderate")
            self.assertEqual(payload["forecast_status"], expected_status)
            self.assertIsNone(payload["forecast_is_warming_up"])

    def test_health_advisory_all_does_not_crash(self):
        client, main = self._client()
        p1, p2, p3 = self._stored_only(main)
        with p1, p2, p3:
            resp = client.get("/api/health-advisory")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        self.assertTrue(rows)
        delhi = next(r for r in rows if r["point"] == "Delhi")
        self.assertEqual(delhi["forecast_status"], "stored")


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


class TestEnforcementRanking(FallbackTestCase):
    """Regression tests for /api/enforcement showing null AQI + hardcoded 150.

    Root cause: the endpoint read `current_aqi` from `fc["current_aqi"]` (None
    for stored forecasts) and did `fc["data_maturity"]["is_warming_up"]`, which
    raised for stored forecasts and wiped BOTH values to None; the ranking then
    fell back to the constant 150."""

    def _client(self):
        from fastapi.testclient import TestClient
        from app import main
        return TestClient(main.app), main

    @staticmethod
    def _attr(point):
        return {
            "point": point,
            "sources": {"traffic_pct": 40.0, "industrial_pct": 20.0,
                        "construction_pct": 5.0, "waste_burning_pct": 5.0,
                        "background_pct": 30.0},
            "raw_signals": {"congestion_ratio": 0.5, "wind_speed_mps": 1.0,
                            "wind_deg": 0, "is_stubble_burning_season": False},
        }

    def test_uses_real_current_aqi_and_forecast(self):
        client, main = self._client()
        # Stored forecast: current_aqi None and data_maturity None (the state
        # that used to crash the row). Current AQI must come from get_current.
        fc = {"forecasts": [{"horizon_hours": 24, "predicted_aqi": 260.0}],
              "current_aqi": None, "data_maturity": None,
              "data_origin": "database_stored_forecast"}
        with patch.object(main, "source_attribution_all", return_value=[self._attr("Delhi")]), \
                patch.object(main, "get_current", return_value={"aqi": 152.0, "category": "Moderate"}), \
                patch.object(main, "forecast", return_value=fc), \
                patch.object(main.db_service, "persist_enforcement", return_value=0):
            resp = client.get("/api/enforcement")
        self.assertEqual(resp.status_code, 200)
        row = resp.json()[0]
        self.assertEqual(row["current_aqi"], 152.0)
        self.assertEqual(row["forecast_aqi_24h"], 260.0)
        self.assertEqual(row["aqi_for_ranking"], 260.0)  # worse of now / +24h
        self.assertIsNone(row["forecast_is_warming_up"])
        self.assertNotEqual(row["aqi_for_ranking"], 150)

    def test_live_forecast_reports_warming_up(self):
        client, main = self._client()
        fc = {"forecasts": [{"horizon_hours": 24, "predicted_aqi": 90.0}],
              "current_aqi": 120.0,
              "data_maturity": {"hours_available": 5, "is_warming_up": True}}
        with patch.object(main, "source_attribution_all", return_value=[self._attr("Delhi")]), \
                patch.object(main, "get_current", return_value={"aqi": 120.0, "category": "Moderate"}), \
                patch.object(main, "forecast", return_value=fc), \
                patch.object(main.db_service, "persist_enforcement", return_value=0):
            resp = client.get("/api/enforcement")
        row = resp.json()[0]
        self.assertTrue(row["forecast_is_warming_up"])
        self.assertEqual(row["current_aqi"], 120.0)
        self.assertEqual(row["forecast_aqi_24h"], 90.0)
        self.assertEqual(row["aqi_for_ranking"], 120.0)  # now worse than +24h

    def test_missing_data_is_null_not_hardcoded_150(self):
        from fastapi import HTTPException

        client, main = self._client()
        with patch.object(main, "source_attribution_all", return_value=[self._attr("Delhi")]), \
                patch.object(main, "get_current",
                             side_effect=HTTPException(503, "no reading")), \
                patch.object(main, "forecast",
                             side_effect=HTTPException(503, "no forecast")), \
                patch.object(main.db_service, "persist_enforcement", return_value=0):
            resp = client.get("/api/enforcement")
        self.assertEqual(resp.status_code, 200)
        row = resp.json()[0]
        self.assertIsNone(row["current_aqi"])
        self.assertIsNone(row["forecast_aqi_24h"])
        self.assertIsNone(row["aqi_for_ranking"])
        self.assertTrue(any("No current or forecasted AQI" in r for r in row["reasons"]))

    def test_ranking_orders_by_real_aqi(self):
        def _cur(city):
            return {"Delhi": 105.0, "Faridabad": 180.0}[city]

        from fastapi import HTTPException

        client, main = self._client()
        with patch.object(main, "source_attribution_all",
                          return_value=[self._attr("Delhi"), self._attr("Faridabad")]), \
                patch.object(main, "get_current",
                             side_effect=lambda city: {"aqi": _cur(city), "category": "Moderate"}), \
                patch.object(main, "forecast",
                             side_effect=HTTPException(503, "no forecast")), \
                patch.object(main.db_service, "persist_enforcement", return_value=0):
            resp = client.get("/api/enforcement")
        rows = resp.json()
        self.assertEqual([r["point"] for r in rows], ["Faridabad", "Delhi"])
        self.assertEqual([r["rank"] for r in rows], [1, 2])
        self.assertEqual(rows[0]["aqi_for_ranking"], 180.0)
        self.assertEqual(rows[1]["aqi_for_ranking"], 105.0)


class TestForecastGrouping(FallbackTestCase):
    """Multiple runs accumulate in aqi_forecasts (one generated_at per run);
    the API must present the LATEST value per horizon, not every run."""

    def _client(self):
        from fastapi.testclient import TestClient
        from app import main
        return TestClient(main.app), main

    def _persist_second_run(self):
        # setUp already persisted one run (24h=210, 48h=230). Add a later run.
        service.persist_forecast("Delhi", {
            "city": "Delhi",
            "reference_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "forecasts": [
                {"horizon_hours": 24, "predicted_aqi": 300.0, "predicted_category": "Poor"},
                {"horizon_hours": 48, "predicted_aqi": 320.0, "predicted_category": "Very Poor"},
            ],
        })

    def test_latest_per_horizon_is_returned(self):
        self._persist_second_run()
        rows = service.query_latest_forecasts("Delhi")
        self.assertEqual([r["horizon_hours"] for r in rows], [24, 48])
        self.assertEqual([r["predicted_aqi"] for r in rows], [300.0, 320.0])

    def test_all_runs_are_preserved_in_the_table(self):
        from sqlalchemy import func, select
        from db import models

        self._persist_second_run()
        session = database.get_session()
        try:
            total = session.execute(
                select(func.count()).select_from(models.AqiForecast)
            ).scalar_one()
        finally:
            session.close()
        self.assertEqual(total, 4)  # both runs kept; only the read collapses

    def test_forecast_endpoint_groups_by_horizon(self):
        self._persist_second_run()
        client, main = self._client()
        with patch.object(main.live_history, "get_live_history",
                          return_value=_empty_live()):
            resp = client.get("/api/forecast/Delhi")
        self.assertEqual(resp.status_code, 200)
        horizons = [f["horizon_hours"] for f in resp.json()["forecasts"]]
        self.assertEqual(horizons, [24, 48])
