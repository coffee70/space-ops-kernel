"""Alembic migration helpers."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings


def get_alembic_config() -> Config:
    """Build an Alembic config bound to the current kernel settings."""

    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "app" / "migrations"))
    config.set_main_option("sqlalchemy.url", get_settings().resolved_database_url)
    return config


def run_migrations(engine=None) -> None:
    """Apply Alembic migrations through head."""

    config = get_alembic_config()
    if engine is None:
        command.upgrade(config, "head")
        return

    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
