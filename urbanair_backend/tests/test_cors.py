"""
CORS configuration tests (offline).

Covers the trailing-slash/whitespace normalisation that previously caused
browser preflight failures when the deployed origin was configured as
``https://<app>.vercel.app/`` while browsers send it without the slash.
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

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from app import main  # noqa: E402

VERCEL_ORIGIN = "https://urban-ckhuhztuo-amelia4.vercel.app"
_CORS_ENV = ("CORS_ORIGINS", "FRONTEND_URL", "CORS_ORIGIN_REGEX")


class CorsEnvMixin(TestCase):
    def setUp(self):
        self._backup = {k: os.environ.get(k) for k in _CORS_ENV}
        for k in _CORS_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestOriginNormalization(CorsEnvMixin):
    def test_strips_whitespace_and_trailing_slashes(self):
        self.assertEqual(main._normalize_origin("  https://x.vercel.app/  "),
                         "https://x.vercel.app")
        self.assertEqual(main._normalize_origin("https://x.vercel.app"),
                         "https://x.vercel.app")
        self.assertEqual(main._normalize_origin("https://x.vercel.app///"),
                         "https://x.vercel.app")
        self.assertEqual(main._normalize_origin("   "), "")

    def test_configured_origin_matches_browser_origin(self):
        # Browser sends the origin WITHOUT a trailing slash.
        os.environ["CORS_ORIGINS"] = f" {VERCEL_ORIGIN}/ "
        self.assertIn(VERCEL_ORIGIN, main._cors_origins())

    def test_multiple_origins_split_and_trimmed(self):
        os.environ["CORS_ORIGINS"] = f"http://localhost:5173, {VERCEL_ORIGIN}/"
        origins = main._cors_origins()
        self.assertIn("http://localhost:5173", origins)
        self.assertIn(VERCEL_ORIGIN, origins)

    def test_frontend_url_is_also_used(self):
        os.environ["FRONTEND_URL"] = f"{VERCEL_ORIGIN}/"
        self.assertIn(VERCEL_ORIGIN, main._cors_origins())

    def test_frontend_url_not_duplicated(self):
        os.environ["CORS_ORIGINS"] = VERCEL_ORIGIN
        os.environ["FRONTEND_URL"] = f"{VERCEL_ORIGIN}/"
        origins = main._cors_origins()
        self.assertEqual(origins.count(VERCEL_ORIGIN), 1)

    def test_defaults_to_local_dev_when_unset(self):
        self.assertIn("http://localhost:5173", main._cors_origins())

    def test_never_wildcards_a_feature(self):
        os.environ["CORS_ORIGINS"] = VERCEL_ORIGIN
        self.assertNotIn("*", main._cors_origins())


class TestCorsMiddlewareConfig(TestCase):
    def test_credentials_true_and_no_wildcard(self):
        cors = [m for m in main.app.user_middleware
                if getattr(m, "cls", None) is CORSMiddleware]
        self.assertTrue(cors, "CORSMiddleware must be installed")
        kwargs = cors[0].kwargs
        self.assertTrue(kwargs.get("allow_credentials") is True)
        self.assertNotIn("*", kwargs.get("allow_origins", []))
