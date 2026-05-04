from __future__ import annotations

import importlib
import json
from pathlib import Path


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


def test_runtime_bootstrapper_excludes_deleted_embedded_demo_application() -> None:
    from app.services.bootstrap_service import BOOTSTRAP_UNITS

    assert "embedded-demo-application" not in BOOTSTRAP_UNITS
