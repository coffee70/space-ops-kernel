"""Migration runner."""

from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules

from sqlalchemy import text

from app.db import Base


def run_migrations(engine) -> None:
    """Apply ordered migration modules once."""

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(255) PRIMARY KEY,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        applied = {
            row[0]
            for row in connection.execute(text("SELECT version FROM schema_migrations")).fetchall()
        }

    versions_pkg = "app.migrations.versions"
    for module_info in sorted(iter_modules(import_module(versions_pkg).__path__), key=lambda item: item.name):
        module = import_module(f"{versions_pkg}.{module_info.name}")
        version = getattr(module, "VERSION")
        if version in applied:
            continue
        module.upgrade(engine)
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO schema_migrations(version) VALUES (:version)"),
                {"version": version},
            )

    Base.metadata.create_all(bind=engine)
