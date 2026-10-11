"""
UrbanAir AI — database package.

Provides optional persistence on Supabase PostgreSQL (via SQLAlchemy).
Everything in this package degrades gracefully: if DATABASE_URL is not
configured, or the database is unreachable, the rest of the application
keeps working exactly as before (reading the live CSVs).
"""

from . import database  # noqa: F401
from . import models  # noqa: F401
from . import repository  # noqa: F401
from . import service  # noqa: F401

__all__ = ["database", "models", "repository", "service"]
