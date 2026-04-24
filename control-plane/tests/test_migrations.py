from __future__ import annotations

from pathlib import Path


def test_only_single_baseline_migration_exists() -> None:
    versions_dir = Path(__file__).resolve().parents[1] / "app" / "migrations" / "versions"
    migration_names = sorted(
        path.name
        for path in versions_dir.glob("*.py")
        if path.name != "__init__.py"
    )

    assert migration_names == ["001_initial_schema.py"]


def test_baseline_migration_is_static_and_does_not_import_live_metadata() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "migrations"
        / "versions"
        / "001_initial_schema.py"
    )
    contents = migration_path.read_text(encoding="utf-8")

    assert "Base.metadata.create_all" not in contents
    assert "Base.metadata.drop_all" not in contents
    assert "import app.models.runtime" not in contents
    assert "from app.db import Base" not in contents


def test_runtime_code_does_not_create_schema_directly() -> None:
    project_root = Path(__file__).resolve().parents[1]
    runtime_sources = [
        project_root / "app" / "db.py",
        project_root / "app" / "registry" / "service.py",
        project_root / "app" / "migrations" / "__init__.py",
    ]

    for source in runtime_sources:
        contents = source.read_text(encoding="utf-8")
        assert "create_all(" not in contents
        assert "drop_all(" not in contents
