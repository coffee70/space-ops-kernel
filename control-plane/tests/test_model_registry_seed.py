"""Tests for seeding workspace shared models.local.yaml from the platform example file."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def test_ensure_runtime_dirs_seeds_model_registry_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    platform_example = (
        workspace / "space-ops-platform/backend/services/agent-runtime-service/config/models.local.yaml.example"
    )
    platform_example.parent.mkdir(parents=True, exist_ok=True)
    platform_example.write_text("version: 1\n", encoding="utf-8")

    settings = Settings(database_url="postgresql://u:p@localhost:5432/db", workspace_root=workspace)
    settings.ensure_runtime_dirs()

    target = workspace / settings.platform_models_registry_host_relpath / settings.platform_models_registry_filename
    assert target.read_text(encoding="utf-8") == "version: 1\n"


def test_ensure_runtime_dirs_does_not_overwrite_existing_model_registry_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "space-ops-kernel/runtime/model-registry/models.local.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("custom: true\n", encoding="utf-8")

    platform_example = (
        workspace / "space-ops-platform/backend/services/agent-runtime-service/config/models.local.yaml.example"
    )
    platform_example.parent.mkdir(parents=True, exist_ok=True)
    platform_example.write_text("version: 1\n", encoding="utf-8")

    settings = Settings(database_url="postgresql://u:p@localhost:5432/db", workspace_root=workspace)
    settings.ensure_runtime_dirs()

    assert target.read_text(encoding="utf-8") == "custom: true\n"


def test_ensure_model_registry_file_raises_when_example_missing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    settings = Settings(database_url="postgresql://u:p@localhost:5432/db", workspace_root=workspace)
    for path in (
        settings.resolved_runtime_root,
        settings.generated_compose_root,
        settings.deployment_logs_root,
        workspace / settings.platform_models_registry_host_relpath,
    ):
        path.mkdir(parents=True, exist_ok=True)

    try:
        settings.ensure_model_registry_file()
    except FileNotFoundError as exc:
        assert "models.local.yaml.example" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError when example file is missing")

