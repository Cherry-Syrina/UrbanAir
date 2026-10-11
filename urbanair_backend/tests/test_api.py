"""
API tests (offline). Uses FastAPI's TestClient against the real app with
the database intentionally NOT configured, verifying that existing
endpoints keep working in CSV-only mode and that DB endpoints fail clearly.
"""

import os
import sys
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.pop("DATABASE_URL", None)
os.environ.pop("SUPABASE_DB_URL", None)

from db import database  # noqa: E402

database.reset()

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


class TestAPI(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        database.reset()

    def test_health_ok(self):
        r = TestClient(app).get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")
        self.assertFalse(r.json()["database"]["configured"])

    def test_cities(self):
        r = TestClient(app).get("/api/cities")
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(len(r.json()), 5)

    def test_db_health_when_unconfigured(self):
        r = TestClient(app).get("/api/db/health")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["configured"])

    def test_aqi_history_503_when_unconfigured(self):
        r = TestClient(app).get("/api/aqi/history/Delhi")
        self.assertEqual(r.status_code, 503)

    def test_geojson_parks(self):
        r = TestClient(app).get("/api/geojson/parks")
        self.assertEqual(r.status_code, 200)
        self.assertIn("features", r.json())

    def test_unknown_city_404(self):
        r = TestClient(app).get("/api/current/Atlantis")
        self.assertEqual(r.status_code, 404)

    def test_nowcast_ml_inference(self):
        r = TestClient(app).post("/api/nowcast", json={
            "city": "Delhi", "co": 0.5, "no2": 20, "o3": 30,
            "pm10": 100, "pm25": 70, "so2": 5,
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("aqi", body)
        self.assertIn("category", body)
