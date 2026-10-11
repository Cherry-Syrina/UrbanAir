"""
Historical-import test: verifies scripts/import_historical.run_import
validates, imports, dedups, and reports counts. Offline (SQLite) with a
small synthetic CSV (NOT the full 219k-row dataset).
"""

import os
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, service  # noqa: E402
from scripts import import_historical  # noqa: E402

HEADER = ("timestamp,location_name,location_lat,location_lon,co,no2,o3,pm10,pm25,so2,"
          "aqi,aqi_category,hour,day,month,year,day_of_week,is_weekend\n")


class TestHistoricalImport(TestCase):
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

    def test_import_skips_invalid_and_dedups(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "processed_aqi_data.csv"
            csv_path.write_text(
                HEADER
                + "2019-01-01 00:00:00,Delhi,28.7041,77.1025,0.5,24.9,18.0,108.2,75.1,4.3,221.6,Poor,0,1,1,2019,1,0\n"
                + "2019-01-01 00:00:00,Faridabad,28.4089,77.3178,1.0,5.6,53.6,56.2,42.7,6.4,137.1,Moderate,0,1,1,2019,1,0\n"
                + "2019-01-01 01:00:00,Delhi,28.7041,77.1025,0.5,24.9,18.0,108.2,75.1,4.3,,,1,1,1,2019,1,0\n"  # invalid: no aqi
            )
            result = import_historical.run_import(csv_path)
            self.assertEqual(result["imported"], 2)
            self.assertEqual(result["invalid"], 1)

            # Re-import -> everything already present -> skipped
            second = import_historical.run_import(csv_path)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["skipped"], 2)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "processed_aqi_data.csv"
            csv_path.write_text(
                HEADER
                + "2019-01-01 00:00:00,Delhi,28.7041,77.1025,0.5,24.9,18.0,108.2,75.1,4.3,221.6,Poor,0,1,1,2019,1,0\n"
            )
            result = import_historical.run_import(csv_path, dry_run=True)
            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["valid"], 1)
            data = service.query_aqi_history("Delhi", hours=10 ** 7, limit=10, offset=0)
            self.assertEqual(data["total"], 0)
