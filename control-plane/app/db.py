"""Database helpers."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

Base = declarative_base()

_engine = None
_SessionLocal = None


def ensure_database_exists(database_url: str | None = None) -> None:
    """Create the configured Postgres database when a reused local volume is missing it."""

    url = make_url(database_url or get_settings().resolved_database_url)
    database_name = url.database
    if not database_name or not url.drivername.startswith("postgresql"):
        return

    admin_engine = create_engine(
        url.set(database="postgres"),
        future=True,
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    try:
        with admin_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            ).scalar()
            if exists:
                return
            quoted_name = database_name.replace('"', '""')
            connection.execute(text(f'CREATE DATABASE "{quoted_name}"'))
    finally:
        admin_engine.dispose()


def get_engine():
    """Return the shared engine."""

    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.resolved_database_url,
            future=True,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory():
    """Return the shared session factory."""

    global _SessionLocal
    if _SessionLocal is None:
        settings = get_settings()
        settings.ensure_runtime_dirs()
        _SessionLocal = sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)
    return _SessionLocal


def get_db():
    """FastAPI dependency."""

    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
