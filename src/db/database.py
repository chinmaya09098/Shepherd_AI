"""
Database engine / session management for Shepherd AI.

Connection is built from Config.DATABASE_URL, or assembled from individual
POSTGRES_* settings when the URL is not provided. Targets Azure Database for
PostgreSQL, so `sslmode=require` is appended automatically when missing.

If no database is configured every helper degrades gracefully (returns None /
False) so the rest of the application continues to run without persistence.
"""
from contextlib import contextmanager
from typing import Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session

from src.config import Config
from src.db.models import Base
from src.utils.logger import get_logger

logger = get_logger(__name__)

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None
_initialized: bool = False


def build_database_url() -> Optional[str]:
    """
    Resolve a SQLAlchemy connection URL from configuration.

    Priority:
      1. Config.DATABASE_URL (full URL)
      2. POSTGRES_HOST/DB/USER/PASSWORD assembled into a URL

    Returns None when nothing is configured.
    """
    url: Optional[str] = None

    if Config.DATABASE_URL:
        url = Config.DATABASE_URL
    elif all([Config.POSTGRES_HOST, Config.POSTGRES_DB,
              Config.POSTGRES_USER, Config.POSTGRES_PASSWORD]):
        user = quote_plus(Config.POSTGRES_USER)
        pwd = quote_plus(Config.POSTGRES_PASSWORD)
        port = Config.POSTGRES_PORT or "5432"
        url = (
            f"postgresql+psycopg2://{user}:{pwd}"
            f"@{Config.POSTGRES_HOST}:{port}/{Config.POSTGRES_DB}"
        )

    if not url:
        return None

    # Normalise the driver so a bare postgresql:// URL uses psycopg2.
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)

    # Azure Database for PostgreSQL requires SSL.
    if "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}sslmode=require"

    return url


def is_db_enabled() -> bool:
    """True when a database connection is configured."""
    return build_database_url() is not None


def get_engine() -> Optional[Engine]:
    """Return a lazily-created, cached SQLAlchemy engine (or None if unconfigured)."""
    global _engine, _SessionFactory

    if _engine is not None:
        return _engine

    url = build_database_url()
    if not url:
        logger.warning(
            "PostgreSQL not configured (no DATABASE_URL / POSTGRES_* vars) — "
            "email records will not be persisted."
        )
        return None

    _engine = create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        # Connection pool tuning — configurable via env vars (see Config).
        # Defaults: pool_size=5, max_overflow=10, pool_timeout=30, pool_recycle=300.
        # Increase pool_size / max_overflow for high-concurrency Function App instances.
        pool_size=Config.DB_POOL_SIZE,
        max_overflow=Config.DB_MAX_OVERFLOW,
        pool_timeout=Config.DB_POOL_TIMEOUT,
        pool_recycle=Config.DB_POOL_RECYCLE,
    )
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    logger.info(
        "PostgreSQL engine created (pool_size=%d max_overflow=%d pool_recycle=%ds)",
        Config.DB_POOL_SIZE,
        Config.DB_MAX_OVERFLOW,
        Config.DB_POOL_RECYCLE,
    )
    return _engine


def init_db() -> bool:
    """
    Ensure tables exist (CREATE TABLE IF NOT EXISTS). Idempotent and safe to call
    repeatedly — only runs the DDL once per process.

    Returns True if the database is ready, False if unconfigured or DDL failed.
    """
    global _initialized

    if _initialized:
        return True

    engine = get_engine()
    if engine is None:
        return False

    try:
        Base.metadata.create_all(engine)
        _initialized = True
        logger.info("PostgreSQL schema ensured (table: email_records)")
        return True
    except Exception as e:
        logger.error(f"Failed to initialise PostgreSQL schema: {e}")
        return False


@contextmanager
def get_session():
    """Context manager yielding a Session. Raises if the DB is not configured."""
    if get_engine() is None:
        raise RuntimeError("Database is not configured")
    session = _SessionFactory()  # type: ignore[misc]
    try:
        yield session
    finally:
        session.close()
