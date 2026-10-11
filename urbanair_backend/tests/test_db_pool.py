"""Connection-pool and session-lifecycle guards.

Supabase counts every client against a shared limit, so the backend must:
  * build exactly ONE engine per process (even under concurrent requests),
  * keep the pool small and bounded,
  * release every connection/session, including on error paths,
  * let one-shot scripts (CI sync) avoid pooling entirely.

These tests never open a real network connection.
"""

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest import TestCase

from sqlalchemy import text
from sqlalchemy.pool import NullPool, QueuePool

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from db import database, service  # noqa: E402

PG_URL = "postgresql+psycopg://user:pass@aws-0-region.pooler.supabase.com:5432/postgres"
_POOL_ENV = ("DB_POOL_MODE", "DB_USE_NULL_POOL", "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT")


class PoolTestCase(TestCase):
    def setUp(self):
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("SUPABASE_DB_URL", None)
        for key in _POOL_ENV:
            os.environ.pop(key, None)
        database.reset()

    def tearDown(self):
        database.reset()
        for key in _POOL_ENV:
            os.environ.pop(key, None)


class TestEngineReuse(PoolTestCase):
    def test_configure_is_idempotent(self):
        self.assertTrue(database.configure("sqlite://", force=True))
        engine1 = database.get_engine()
        self.assertTrue(database.configure("sqlite://"))
        self.assertIs(engine1, database.get_engine())

    def test_force_disposes_and_recreates(self):
        database.configure("sqlite://", force=True)
        engine1 = database.get_engine()
        database.configure("sqlite://", force=True)
        self.assertIsNot(engine1, database.get_engine())

    def test_single_engine_under_concurrency(self):
        barrier = threading.Barrier(8)
        seen = []

        def worker():
            barrier.wait()
            database.configure("sqlite://")
            seen.append(id(database.get_engine()))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(seen)), 1)


class TestSessionCleanup(PoolTestCase):
    def setUp(self):
        super().setUp()
        self._dir = tempfile.mkdtemp(prefix="ua_pool_")
        self._db = Path(self._dir) / "test.db"
        self.assertTrue(database.configure(f"sqlite:///{self._db.as_posix()}", force=True))
        database.create_all()

    def tearDown(self):
        database.reset()
        shutil.rmtree(self._dir, ignore_errors=True)
        super().tearDown()

    def test_session_scope_releases_connection(self):
        engine = database.get_engine()
        self.assertEqual(engine.pool.checkedout(), 0)
        with service.session_scope() as session:
            session.execute(text("SELECT 1"))
            self.assertGreaterEqual(engine.pool.checkedout(), 1)
        self.assertEqual(engine.pool.checkedout(), 0)

    def test_session_scope_releases_connection_on_error(self):
        engine = database.get_engine()
        with self.assertRaises(RuntimeError):
            with service.session_scope() as session:
                session.execute(text("SELECT 1"))
                raise RuntimeError("boom")
        self.assertEqual(engine.pool.checkedout(), 0)


class TestPoolConfig(PoolTestCase):
    def test_postgres_pool_is_small_and_bounded(self):
        self.assertTrue(database.configure(PG_URL, force=True))
        engine = database.get_engine()
        self.assertEqual(engine.dialect.driver, "psycopg")
        self.assertIsInstance(engine.pool, QueuePool)
        self.assertEqual(engine.pool._pool.maxsize, 2)
        self.assertEqual(engine.pool._max_overflow, 3)

    def test_pool_size_env_override(self):
        os.environ["DB_POOL_SIZE"] = "1"
        os.environ["DB_MAX_OVERFLOW"] = "2"
        database.configure(PG_URL, force=True)
        engine = database.get_engine()
        self.assertEqual(engine.pool._pool.maxsize, 1)
        self.assertEqual(engine.pool._max_overflow, 2)

    def test_null_pool_explicit(self):
        database.configure(PG_URL, null_pool=True, force=True)
        self.assertIsInstance(database.get_engine().pool, NullPool)

    def test_null_pool_via_env(self):
        os.environ["DB_POOL_MODE"] = "null"
        database.configure(PG_URL, force=True)
        self.assertIsInstance(database.get_engine().pool, NullPool)

    def test_psycopg_scheme_and_ssl_preserved(self):
        database.configure(PG_URL, force=True)
        url = str(database.get_engine().url)
        self.assertTrue(url.startswith("postgresql+psycopg://"))
        self.assertIn("sslmode=require", url)
