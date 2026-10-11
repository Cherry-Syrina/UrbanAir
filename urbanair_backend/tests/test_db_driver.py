"""Lightweight checks that the PostgreSQL driver the app needs is installed.

The backend builds its engine from ``DATABASE_URL``. When that URL uses the
SQLAlchemy ``postgresql+psycopg://`` scheme (Supabase's "SQLAlchemy"
connection string), SQLAlchemy imports the ``psycopg`` (Psycopg 3) package.
If it is missing, ``db.configure()`` fails with
``Failed to configure database engine: No module named 'psycopg'``.

These tests are dependency-only: they never open a network connection and
never read or print credentials.
"""

import importlib
import sys
from pathlib import Path
from unittest import TestCase

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database  # noqa: E402


class TestPostgresDriver(TestCase):
    def test_psycopg3_importable(self):
        psycopg = importlib.import_module("psycopg")
        self.assertTrue(getattr(psycopg, "__version__", ""))

    def test_sqlalchemy_psycopg_dialect_available(self):
        from sqlalchemy import create_engine

        engine = create_engine("postgresql+psycopg://user:pass@localhost:5432/db")
        try:
            self.assertEqual(engine.dialect.name, "postgresql")
            self.assertEqual(engine.dialect.driver, "psycopg")
        finally:
            engine.dispose()

    def test_url_normalization_preserves_psycopg_driver(self):
        out = database._normalize_url(
            "postgresql+psycopg://u:p@aws-0-region.pooler.supabase.com:5432/postgres"
        )
        self.assertTrue(out.startswith("postgresql+psycopg://"))
        self.assertIn("sslmode=require", out)
