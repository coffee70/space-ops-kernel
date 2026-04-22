"""Database helpers."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import get_settings

Base = declarative_base()

_engine = None
_SessionLocal = None


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
