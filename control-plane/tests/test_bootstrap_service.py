from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace


def test_managed_fork_bootstrap_is_non_destructive_after_initial_import(control_plane_env: Path) -> None:
    import app.config
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)

    bootstrapper.ensure_bootstrapped()

    managed_file = settings.main_worktree_dir / "project/space-ops-platform/README.md"
    managed_file.write_text("managed-only change\n", encoding="utf-8")

    mounted_seed_file = control_plane_env / "space-ops-platform" / "README.md"
    mounted_seed_file.write_text("seed changed after bootstrap\n", encoding="utf-8")

    bootstrapper.ensure_bootstrapped()

    assert managed_file.read_text(encoding="utf-8") == "managed-only change\n"


def test_bootstrap_manifest_sync_updates_unchanged_seeded_manifest(control_plane_env: Path, monkeypatch) -> None:
    import app.config
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)
    seed_root = control_plane_env / "kernel-seeds"
    seed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrapper, "_seed_manifests_root", lambda: seed_root)

    seed_path = seed_root / "derived-telemetry-service.yaml"
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 1\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    managed_manifest = settings.main_worktree_dir / "manifests/units/derived-telemetry-service.yaml"
    assert managed_manifest.read_text(encoding="utf-8") == "unit_id: derived-telemetry-service\nversion: 1\n"

    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 2\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    assert managed_manifest.read_text(encoding="utf-8") == "unit_id: derived-telemetry-service\nversion: 2\n"
    state = json.loads(settings.bootstrap_seed_state_path.read_text(encoding="utf-8"))
    assert "manifests/units/derived-telemetry-service.yaml" in state


def test_bootstrap_manifest_sync_preserves_locally_edited_manifest(control_plane_env: Path, monkeypatch) -> None:
    import app.config
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)
    seed_root = control_plane_env / "kernel-seeds"
    seed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrapper, "_seed_manifests_root", lambda: seed_root)

    seed_path = seed_root / "derived-telemetry-service.yaml"
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 1\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    managed_manifest = settings.main_worktree_dir / "manifests/units/derived-telemetry-service.yaml"
    managed_manifest.write_text("unit_id: derived-telemetry-service\nversion: local-edit\n", encoding="utf-8")
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 2\n", encoding="utf-8")

    bootstrapper.ensure_bootstrapped()

    assert managed_manifest.read_text(encoding="utf-8") == "unit_id: derived-telemetry-service\nversion: local-edit\n"


def test_bootstrap_manifest_sync_creates_commit_when_seed_changes(control_plane_env: Path, monkeypatch) -> None:
    import app.config
    from app.git.repository import ManagedGitRepository
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)
    seed_root = control_plane_env / "kernel-seeds"
    seed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrapper, "_seed_manifests_root", lambda: seed_root)

    seed_path = seed_root / "derived-telemetry-service.yaml"
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 1\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    repository = ManagedGitRepository(settings)
    first_commit = repository.get_head_commit("main")

    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 2\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    second_commit = repository.get_head_commit("main")
    history = repository.get_history("main", "manifests/units/derived-telemetry-service.yaml", limit=1)

    assert second_commit != first_commit
    assert history[0]["subject"] == "Sync bootstrap manifests from control plane"


def test_runtime_bootstrapper_units_match_seed_manifest_inventory() -> None:
    from app.services.bootstrap_service import BOOTSTRAP_UNITS

    manifest_root = Path(__file__).resolve().parents[1] / "app" / "bootstrap" / "manifests"
    manifest_unit_ids = tuple(sorted(path.stem for path in manifest_root.glob("*.yaml")))

    assert tuple(sorted(BOOTSTRAP_UNITS)) == manifest_unit_ids


def test_runtime_bootstrapper_continues_after_failed_unit(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    attempted: list[str] = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            return None

    class DummyDeploymentService:
        def __init__(self, settings, repository, session):
            return None

        def submit(self, request, *, delete_eligible):
            attempted.append(request.unit_id)
            if request.unit_id == "bad-unit":
                return SimpleNamespace(
                    deployment_id="dep_bad",
                    status="failed",
                    failure_reason="synthetic failure",
                )
            return SimpleNamespace(deployment_id=f"dep_{request.unit_id}", status="healthy", failure_reason=None)

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("bad-unit", "later-unit"))
    monkeypatch.setattr(bootstrap_service, "ManagedGitRepository", lambda settings: SimpleNamespace(get_head_commit=lambda branch: "abc123"))
    monkeypatch.setattr(bootstrap_service, "get_session_factory", lambda: lambda: DummySession())
    monkeypatch.setattr(bootstrap_service, "DeploymentService", DummyDeploymentService)
    monkeypatch.setattr(bootstrap_service, "RegistryService", lambda session: object())
    bootstrapper = RuntimeBootstrapper(SimpleNamespace(main_worktree_dir=Path("/unused"), runtime_strategy="stub"))
    monkeypatch.setattr(bootstrapper, "_current_deployment_id", lambda registry, service, unit_id, commit_sha: None)
    monkeypatch.setattr(bootstrapper, "_bootstrap_source_exists", lambda service, unit_id, commit_sha: True)

    result = bootstrapper.ensure_bootstrapped(fail_fast=False)

    assert attempted == ["bad-unit", "later-unit"]
    assert [(unit.unit_id, unit.status, unit.deployment_id) for unit in result.units] == [
        ("bad-unit", "failed", "dep_bad"),
        ("later-unit", "healthy", "dep_later-unit"),
    ]


def test_runtime_bootstrapper_records_current_and_skipped_units(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            return None

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("current-unit", "missing-unit"))
    monkeypatch.setattr(bootstrap_service, "ManagedGitRepository", lambda settings: SimpleNamespace(get_head_commit=lambda branch: "abc123"))
    monkeypatch.setattr(bootstrap_service, "get_session_factory", lambda: lambda: DummySession())
    monkeypatch.setattr(bootstrap_service, "DeploymentService", lambda settings, repository, session: object())
    monkeypatch.setattr(bootstrap_service, "RegistryService", lambda session: object())
    bootstrapper = RuntimeBootstrapper(SimpleNamespace(main_worktree_dir=Path("/unused"), runtime_strategy="stub"))
    monkeypatch.setattr(
        bootstrapper,
        "_current_deployment_id",
        lambda registry, service, unit_id, commit_sha: "dep_current" if unit_id == "current-unit" else None,
    )
    monkeypatch.setattr(bootstrapper, "_bootstrap_source_exists", lambda service, unit_id, commit_sha: False)

    result = bootstrapper.ensure_bootstrapped(fail_fast=False)

    assert [(unit.unit_id, unit.status, unit.deployment_id, unit.failure_reason) for unit in result.units] == [
        ("current-unit", "current", "dep_current", None),
        ("missing-unit", "skipped", None, "bootstrap source missing"),
    ]
