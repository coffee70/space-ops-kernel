from __future__ import annotations

import importlib
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
