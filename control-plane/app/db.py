"""Database helpers."""

from __future__ import annotations

from sqlalchemy import create_engine, event
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
        connect_args = {}
        engine_kwargs = {
            "future": True,
            "pool_pre_ping": True,
        }
        if settings.resolved_database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = 30
        _engine = create_engine(
            settings.resolved_database_url,
            connect_args=connect_args,
            **engine_kwargs,
        )
        if settings.resolved_database_url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def configure_sqlite(connection, _):  # type: ignore[no-redef]
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
    return _engine


def get_session_factory():
    """Return the shared session factory."""

    global _SessionLocal
    if _SessionLocal is None:
        from app.migrations import run_migrations

        settings = get_settings()
        settings.ensure_runtime_dirs()
        run_migrations(get_engine())
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
