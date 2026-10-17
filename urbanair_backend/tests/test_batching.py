"""
Regression tests for the N+1 database-session stall on the all-points endpoints.

Root cause: /api/enforcement and /api/health-advisory loop over every monitored
point (21) and called get_current()/forecast() once per point. Each of those
opened its own ``session_scope()`` -- ~5 pooled connections per point (~105
total) -- and against the remote Supabase pooler each connection checkout costs
~1s, so a single request took ~85s and the browser (20s timeout) never saw it
finish.

Fix: preload the latest stored reading + pollutant row + forecasts for every
needed city in ONE connection (db.service.load_stored_snapshot) and reuse it.

These tests assert the number of DB sessions does NOT scale with the number of
points, and that the payloads are unchanged.
"""

import sys
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import source_attribution as sa  # noqa: E402
from db import database, service  # noqa: E402
from tests.test_fallback import FallbackTestCase, _empty_live, _iso_now  # noqa: E402


def _source_estimate(point):
    return {
        "point": point,
        "reference_time": _iso_now(),
        "sources": {"traffic_pct": 30.0, "industrial_pct": 20.0,
                    "construction_pct": 10.0, "waste_burning_pct": 5.0,
                    "background_pct": 35.0},
        "raw_signals": {"congestion_ratio": 0.6, "wind_speed_mps": 2.0,
                        "wind_deg": 300, "is_stubble_burning_season": False},
    }


class BatchingTestCase(FallbackTestCase):
    def _client(self):
        from fastapi.testclient import TestClient
        from app import main
        return TestClient(main.app), main

    def _stored_only(self, main):
        """Force the no-live-CSV path the deployed container actually takes."""
        return (
            patch.object(main, "_load_latest_live_pollutants", return_value=None),
            patch.object(main, "_load_latest_ground_aqi", return_value=None),
            patch.object(main.live_history, "get_live_history", return_value=_empty_live()),
            patch.object(main.sa, "run_for_all_points",
                         side_effect=FileNotFoundError("no collector CSVs")),
        )

    def _count_sessions(self, main, call):
        """Run `call()` while counting every ``service.session_scope`` checkout."""
        counter = {"n": 0}
        real_scope = service.session_scope

        def counting_scope():
            counter["n"] += 1
            return real_scope()

        patches = self._stored_only(main)
        with patch.object(service, "session_scope", counting_scope), \
                patches[0], patches[1], patches[2], patches[3]:
            result = call()
        return counter["n"], result


class TestSessionsDoNotScaleWithPoints(BatchingTestCase):
    def test_enforcement_sessions_are_flat_as_points_grow(self):
        client, main = self._client()
        all_points = list(sa.CITY_COORDS)
        small = all_points[:2]
        large = all_points[:10]

        service.persist_source_attribution([_source_estimate(p) for p in small])
        n_small, resp_small = self._count_sessions(main, lambda: client.get("/api/enforcement"))
        self.assertEqual(resp_small.status_code, 200)

        service.persist_source_attribution([_source_estimate(p) for p in large])
        n_large, resp_large = self._count_sessions(main, lambda: client.get("/api/enforcement"))
        self.assertEqual(resp_large.status_code, 200)

        # 10 points, but the same number of connections as 2 points: the loop
        # must NOT open a connection per point (the old bug: ~5 per point).
        self.assertEqual(n_small, n_large)
        self.assertLessEqual(n_large, 6)

    def test_health_advisory_all_uses_a_single_snapshot_connection(self):
        client, main = self._client()
        service.persist_source_attribution([_source_estimate(p) for p in sa.CITY_COORDS])
        n, resp = self._count_sessions(main, lambda: client.get("/api/health-advisory"))
        self.assertEqual(resp.status_code, 200)
        # snapshot load only -- no per-point connections.
        self.assertLessEqual(n, 2)


class TestBatchedPayloadIsUnchanged(BatchingTestCase):
    def test_enforcement_returns_real_values_via_snapshot(self):
        client, main = self._client()
        n, resp = self._count_sessions(main, lambda: client.get("/api/enforcement"))
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()
        delhi = next(r for r in rows if r["point"] == "Delhi")
        # seeded in FallbackTestCase.setUp: latest reading 202.0, 24h forecast 210.0
        self.assertEqual(delhi["current_aqi"], 202.0)
        self.assertEqual(delhi["forecast_aqi_24h"], 210.0)
        self.assertEqual(delhi["aqi_for_ranking"], 210.0)  # max(now, +24h)
        self.assertEqual(delhi["rank"], 1)

    def test_health_advisory_all_keeps_expected_shape(self):
        client, main = self._client()
        n, resp = self._count_sessions(main, lambda: client.get("/api/health-advisory"))
        self.assertEqual(resp.status_code, 200)
        delhi = next(r for r in resp.json() if r["point"] == "Delhi")
        self.assertEqual(delhi["current_aqi"], 202.0)
        self.assertEqual(delhi["nearest_known_city"], "Delhi")
        self.assertEqual(delhi["forecast_status"], "stored")
        self.assertIn("general_advisory", delhi)


class TestBulkRepositoryReaders(BatchingTestCase):
    def _seed_reading(self, city, lat, lon, observed_at, aqi, source, pollutants=None):
        from db import repository

        session = database.get_session()
        sid = repository.ensure_station(session, city, lat, lon)
        row = {"station_id": sid, "observed_at": observed_at, "aqi": aqi,
               "aqi_category": "Poor" if aqi is not None else None, "source": source}
        for k in ("co", "no2", "o3", "pm10", "pm25", "so2"):
            row[k] = (pollutants or {}).get(k)
        repository.save_aqi_readings(session, [row])
        session.commit()
        session.close()

    def test_load_stored_snapshot_maps_each_station(self):
        snap = service.load_stored_snapshot(["Delhi", "Faridabad"])
        self.assertEqual(snap["readings"]["Delhi"]["aqi"], 202.0)
        self.assertIsNone(snap["readings"].get("Faridabad"))  # no rows seeded
        self.assertEqual(snap["forecasts"]["Delhi"][0]["horizon_hours"], 24)

    def test_latest_readings_one_row_per_station(self):
        from datetime import datetime, timezone

        from db import repository

        now = datetime.now(timezone.utc)
        self._seed_reading("Delhi", 28.7041, 77.1025, now, 111.0, "waqi")
        session = database.get_session()
        try:
            rows = repository.get_latest_readings(session, ["Delhi", "Faridabad"])
        finally:
            session.close()
        self.assertEqual(len(rows), 1)  # only Delhi exists
        self.assertEqual(rows[0]["aqi"], 111.0)  # newest wins
        self.assertEqual(rows[0]["station"], "Delhi")

    def test_latest_pollutant_readings_skip_aqi_only_rows(self):
        from datetime import datetime, timezone

        from db import repository

        now = datetime.now(timezone.utc)
        self._seed_reading("Delhi", 28.7041, 77.1025, now, 155.0, "waqi")  # no pollutants
        self._seed_reading("Delhi", 28.7041, 77.1025, now, None, "owm_nowcast",
                   pollutants={"co": 0.22, "pm25": 22.3})
        session = database.get_session()
        try:
            rows = repository.get_latest_pollutant_readings(session, ["Delhi"])
        finally:
            session.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "owm_nowcast")
        self.assertEqual(rows[0]["pm25"], 22.3)

    def test_station_ids_bulk_lookup(self):
        from db import repository

        session = database.get_session()
        try:
            ids = repository.get_station_ids(session, ["Delhi", "Nowhere"])
        finally:
            session.close()
        self.assertIn("Delhi", ids)
        self.assertNotIn("Nowhere", ids)
