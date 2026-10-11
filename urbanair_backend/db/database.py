"""
Database connection management (Supabase PostgreSQL via SQLAlchemy).

Design goals
------------
* Optional: if DATABASE_URL / SUPABASE_DB_URL is not set, the app runs on
  the existing CSV pipeline unchanged. Every helper here fails soft.
* Safe: never logs or returns the raw connection string (it can contain
  the database password). Errors are truncated and redacted.
* SSL is required for hosted Postgres. We never disable certificate
  verification to work around connection errors.
* Works with SQLite (used by the offline unit tests) as well as Postgres.

Environment variables
---------------------
DATABASE_URL        Full SQLAlchemy URL, e.g.
                    postgresql://postgres.<ref>:<password>@<host>:6543/postgres?sslmode=require
SUPABASE_DB_URL     Alias for DATABASE_URL (takes lower priority).
DB_ECHO             "1" to echo SQL (dev only). Default off.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

logger = logging.getLogger("urbanair.db")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

Base = declarative_base()

_engine = None
_SessionLocal = None
_configured_url = None


class DatabaseUnavailable(RuntimeError):
    """Raised when a database session is requested but the DB is not usable."""


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def get_database_url() -> Optional[str]:
    return os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or None


def _redact(url: str) -> str:
    """Hide the password portion of a connection URL for safe logging.

    Matches greedily up to the LAST ``@`` so that a password containing
    reserved characters (e.g. an unescaped ``@``) is fully hidden and can
    never leak into logs.
    """
    if not url:
        return ""
    return re.sub(r"(://[^:@/]+:).*(@)", r"\1****\2", url, count=1)


def _normalize_url(url: str) -> str:
    """Make a user-supplied URL usable by SQLAlchemy/psycopg2.

    * map the legacy ``postgres://`` scheme to ``postgresql://``
    * default hosted (non-local) Postgres connections to ``sslmode=require``
      without ever downgrading or disabling verification.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    is_postgres = url.startswith("postgresql://") or url.startswith("postgresql+")
    is_local = any(h in url for h in ("localhost", "127.0.0.1", "@db:", "host.docker.internal"))
    if is_postgres and not is_local and "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"
    return url


def configure(url: Optional[str] = None, *, echo: bool = False, force: bool = False) -> bool:
    """(Re)create the engine + session factory. Returns True if configured."""
    global _engine, _SessionLocal, _configured_url

    url = url or get_database_url()
    if not url:
        _engine = None
        _SessionLocal = None
        _configured_url = None
        return False

    if _engine is not None and not force and url == _configured_url:
        return True

    kwargs = {"future": True, "echo": echo or os.environ.get("DB_ECHO") == "1"}

    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        kwargs["connect_args"] = {"check_same_thread": False}
        # In-memory SQLite needs a shared static pool so all sessions see the
        # same database.
        if ":memory:" in url or url.rstrip("/") == "sqlite:":
            kwargs["poolclass"] = StaticPool
    else:
        kwargs.update(pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5)

    try:
        _engine = create_engine(_normalize_url(url), **kwargs)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
        _configured_url = url
        logger.info("Database configured: %s", _redact(url))
        return True
    except Exception as exc:  # pragma: no cover - configuration error path
        logger.error("Failed to configure database engine: %s", str(exc)[:200])
        _engine = None
        _SessionLocal = None
        _configured_url = None
        return False


def is_configured() -> bool:
    return _engine is not None or bool(get_database_url())


def get_engine():
    if _engine is None and get_database_url():
        configure()
    return _engine


def get_session():
    engine = get_engine()
    if engine is None or _SessionLocal is None:
        raise DatabaseUnavailable("DATABASE_URL is not configured.")
    return _SessionLocal()


def create_all() -> bool:
    """Create any missing tables. Never drops or alters existing ones."""
    engine = get_engine()
    if engine is None:
        return False
    from . import models  # noqa: F401  (register metadata)

    Base.metadata.create_all(bind=engine)
    return True


def health() -> dict:
    """Return a JSON-safe health snapshot. Never includes the password."""
    url = get_database_url()
    if _engine is None and not url:
        return {
            "configured": False,
            "connected": False,
            "dialect": None,
            "message": "DATABASE_URL / SUPABASE_DB_URL not set — running in CSV-only mode.",
        }
    try:
        engine = get_engine()
        if engine is None:
            return {"configured": True, "connected": False, "dialect": None,
                    "message": "Engine could not be created."}
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"configured": True, "connected": True, "dialect": engine.dialect.name,
                "host": engine.url.host}
    except Exception as exc:
        engine = get_engine()
        return {
            "configured": True,
            "connected": False,
            "dialect": (engine.dialect.name if engine is not None else None),
            "message": f"Connection failed: {str(exc)[:180]}",
        }


def reset() -> None:
    """Tear down the engine (used by tests)."""
    global _engine, _SessionLocal, _configured_url
    if _engine is not None:
        try:
            _engine.dispose()
        except Exception:  # pragma: no cover
            pass
    _engine = None
    _SessionLocal = None
    _configured_url = None
