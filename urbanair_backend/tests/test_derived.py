"""
Tests for the derived-artifact pipeline (forecasts + source attribution).

Context: the deployed web service has no collector CSVs, so /api/forecast,
/api/health-advisory and /api/source-attribution read persisted tables. Nothing
populated those tables (only readings were synced), which is why they returned
503. These tests lock in that `scripts.persist_derived` -- run in the same job
that collects the CSVs -- actually writes those rows from real CSV input, and
that `forecast_engine` produces the same payload shape the endpoint serves.

Offline: in-memory SQLite + a temp CSV dir; no Supabase, no network.
"""

import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase

import pandas as pd

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, service  # noqa: E402
import forecast_engine  # noqa: E402
from scripts import persist_derived  # noqa: E402


def _synthetic_city_df(n=40):
    """A live-history-shaped frame for one city (the columns the forecast
    feature builder reads: timestamp/aqi/hour/day_of_week/month/year/...)."""
    base = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    timestamps = [base + timedelta(hours=i) for i in range(n)]
    df = pd.DataFrame({
        "timestamp": timestamps,
        "location_name": ["Delhi"] * n,
        "location_lat": [28.7041] * n,
        "location_lon": [77.1025] * n,
        "pm25": [75.0] * n,
        "pm10": [108.0] * n,
        "aqi": [200.0 + (i % 5) for i in range(n)],
        "aqi_category": ["Poor"] * n,
    })
    df["hour"] = df["timestamp"].dt.hour
    df["day_of_week"] = df["timestamp"].dt.dayofweek
    df["month"] = df["timestamp"].dt.month
    df["year"] = df["timestamp"].dt.year
    df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
    return df


class TestForecastEngine(TestCase):
    def test_payload_shape_matches_endpoint(self):
        payload = forecast_engine.build_forecast_payload("Delhi", _synthetic_city_df())
        self.assertEqual(payload["city"], "Delhi")
        self.assertEqual([f["horizon_hours"] for f in payload["forecasts"]], [24, 48, 72])
        self.assertIsNotNone(payload["current_aqi"])
        self.assertEqual(payload["data_maturity"]["hours_needed_for_full_accuracy"], 168)
        self.assertTrue(payload["data_maturity"]["is_warming_up"])
        self.assertIn("model_metrics", payload)
        for f in payload["forecasts"]:
            self.assertIsNotNone(f["predicted_aqi"])
            self.assertIn(f["predicted_category"], forecast_engine.AQI_CATEGORY_COLORS)


class TestForecastEngineDependencies(TestCase):
    """Guards the exact failure seen in CI: forecast_engine unpickles
    models_forecast/city_encoder.pkl, a scikit-learn LabelEncoder, which
    requires scikit-learn to be installed at a compatible version."""

    def test_city_encoder_loads_and_transforms(self):
        encoder = forecast_engine._load_artifacts()[1]
        codes = encoder.transform(["Delhi"])
        self.assertEqual(len(codes), 1)

    def test_requirements_pin_matches_artifact_sklearn_version(self):
        pinned = None
        for raw in (BACKEND_DIR / "requirements.txt").read_text().splitlines():
            line = raw.strip()
            if line.startswith("scikit-learn"):
                self.assertIn("==", line,
                              "scikit-learn must be pinned to the artifact's version")
                pinned = line.split("==", 1)[1].split(";")[0].strip()
        self.assertIsNotNone(pinned, "requirements.txt must declare scikit-learn")

        raw = (forecast_engine.MODELS_FORECAST_DIR / "city_encoder.pkl").read_bytes()
        recorded = {m.decode() for m in re.findall(rb"\b\d+\.\d+\.\d+\b", raw)}
        self.assertIn(pinned, recorded,
                      f"pinned scikit-learn {pinned} does not match the version "
                      f"the encoder was saved with {sorted(recorded)}")


class TestPersistDerived(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        os.environ.pop("URBANAIR_LIVE_CSV_DIR", None)
        database.reset()
        self.assertTrue(database.configure("sqlite://", force=True))
        database.create_all()
        service._schema_ready = True
        self._tmp = Path(tempfile.mkdtemp(prefix="ua_derived_"))
        self._write_ground_aqi_csv()
        self._write_traffic_weather_csvs()

    def tearDown(self):
        os.environ.pop("URBANAIR_LIVE_CSV_DIR", None)
        database.reset()
        service._schema_ready = False
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_ground_aqi_csv(self):
        base = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        pd.DataFrame([{
            "timestamp": (base + timedelta(hours=i)).isoformat(),
            "location_name": "Delhi",
            "location_lat": 28.7041,
            "location_lon": 77.1025,
            "aqi": 200.0 + (i % 5),
            "aqi_category": "Poor",
        } for i in range(40)]).to_csv(self._tmp / "live_ground_aqi_log.csv", index=False)

    def _write_traffic_weather_csvs(self):
        pd.DataFrame([{
            "timestamp": "2024-01-01T00:00:00+00:00",
            "point_id": "delhi_1", "type": "arterial", "name": "Delhi",
            "lat": 28.7041, "lon": 77.1025, "congestion_ratio": 0.6,
        }]).to_csv(self._tmp / "traffic_log.csv", index=False)
        pd.DataFrame([{
            "fetch_timestamp": "2024-01-01T00:00:00+00:00",
            "city": "Delhi",
            "lat": 28.7041, "lon": 77.1025, "wind_speed": 2.0, "wind_deg": 300.0,
        }]).to_csv(self._tmp / "weather_current_log.csv", index=False)

    def test_persist_derived_writes_forecasts_and_attribution(self):
        result = persist_derived.persist_derived(self._tmp, cities=["Delhi"])
        self.assertTrue(result["configured"])
        self.assertEqual(result["forecasts_built"], 3)
        self.assertGreater(result["forecasts_persisted"], 0)
        self.assertGreater(result["attribution_points"], 0)
        self.assertGreater(result["attribution_persisted"], 0)
        self.assertIn("Delhi", result["cities"])

        stored_forecasts = service.query_latest_forecasts("Delhi")
        self.assertTrue(stored_forecasts)
        self.assertEqual(
            sorted({f["horizon_hours"] for f in stored_forecasts}), [24, 48, 72])

        stored_attr = service.query_latest_source_estimates()
        self.assertTrue(stored_attr)
        self.assertTrue(any(s["point"] == "Delhi" for s in stored_attr))

    def test_persist_derived_skips_when_csv_missing(self):
        empty = Path(tempfile.mkdtemp(dir=str(self._tmp)))
        result = persist_derived.persist_derived(empty, cities=["Delhi"])
        self.assertEqual(result["forecasts_built"], 0)
        self.assertEqual(result["cities"], [])
        self.assertIsNotNone(result["attribution_error"])
