"""Managed fork bootstrap."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from app.config import Settings
from app.services.shell import run_command

IGNORE_NAMES = shutil.ignore_patterns(
    ".git",
    ".venv",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
    ".next",
    "dist",
    "build",
    "tmp",
)


class ManagedForkBootstrapper:
    """Create and seed the managed fork."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_bootstrapped(self) -> None:
        """Create the bare repo and main worktree if missing."""

        self.settings.ensure_runtime_dirs()
        if self.settings.bare_repo_dir.exists() and self.settings.main_worktree_dir.exists():
            return

        self.settings.bare_repo_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(dir=self.settings.resolved_runtime_root) as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._materialize_import_tree(temp_dir)
            run_command(["git", "init", "-b", "main"], cwd=temp_dir)
            run_command(["git", "config", "user.name", "Space Ops Control Plane"], cwd=temp_dir)
            run_command(["git", "config", "user.email", "control-plane@space-ops.local"], cwd=temp_dir)
            run_command(["git", "add", "."], cwd=temp_dir)
            run_command(["git", "commit", "-m", "Initial managed fork import"], cwd=temp_dir)

            if not any(self.settings.bare_repo_dir.iterdir()):
                run_command(["git", "init", "--bare", str(self.settings.bare_repo_dir)])

            run_command(["git", "remote", "add", "origin", str(self.settings.bare_repo_dir)], cwd=temp_dir)
            run_command(["git", "push", "--force", "origin", "main"], cwd=temp_dir)

        if self.settings.main_worktree_dir.exists():
            shutil.rmtree(self.settings.main_worktree_dir)
        run_command(
            [
                "git",
                f"--git-dir={self.settings.bare_repo_dir}",
                "worktree",
                "add",
                str(self.settings.main_worktree_dir),
                "main",
            ]
        )

    def _materialize_import_tree(self, root: Path) -> None:
        project_root = root / "project"
        manifests_root = root / "manifests" / "units"
        project_root.mkdir(parents=True, exist_ok=True)
        manifests_root.mkdir(parents=True, exist_ok=True)

        shutil.copytree(
            self.settings.resolved_platform_source_root,
            project_root / "space-ops-platform",
            ignore=IGNORE_NAMES,
            dirs_exist_ok=True,
        )
        shutil.copytree(
            self.settings.resolved_apps_source_root,
            project_root / "space-ops-apps",
            ignore=IGNORE_NAMES,
            dirs_exist_ok=True,
        )

        seed_root = Path(__file__).resolve().parents[1] / "bootstrap" / "manifests"
        for manifest_path in seed_root.glob("*.yaml"):
            shutil.copy2(manifest_path, manifests_root / manifest_path.name)

