"""Managed fork and runtime bootstrap."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import httpx

from app.config import Settings
from app.db import get_session_factory
from app.deployments.service import DeploymentService
from app.git.repository import ManagedGitRepository
from app.registry.service import RegistryService
from app.schemas import DeploymentSubmissionRequest, RuntimeRef
from app.services.proxy_targets import build_runtime_health_url
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

BOOTSTRAP_UNITS = (
    "vehicle-config-service",
    "source-registry-service",
    "telemetry-ingest-service",
    "position-orbit-service",
    "simulator-control-service",
    "telemetry-query-service",
    "telemetry-intelligence-service",
    "ops-events-service",
    "platform-api-gateway",
    "derived-telemetry-service",
)


class ManagedForkBootstrapper:
    """Create the managed fork and preserve it as durable editable state."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_bootstrapped(self) -> None:
        """Ensure the managed fork exists without overwriting existing managed code."""

        self.settings.ensure_runtime_dirs()
        repo_missing = not self.settings.bare_repo_dir.exists() or not any(self.settings.bare_repo_dir.iterdir())
        if repo_missing:
            self._initialize_managed_repo()
        self._ensure_main_worktree()

    def _initialize_managed_repo(self) -> None:
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

    def _ensure_main_worktree(self) -> None:
        if self.settings.main_worktree_dir.exists():
            return
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


class RuntimeBootstrapper:
    """Ensure required managed units are deployed for the local stack."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_bootstrapped(self) -> None:
        repository = ManagedGitRepository(self.settings)
        commit_sha = repository.get_head_commit("main")
        session_factory = get_session_factory()

        with session_factory() as session:
            deployment_service = DeploymentService(self.settings, repository, session)
            registry = RegistryService(session)

            for unit_id in BOOTSTRAP_UNITS:
                if self._deployment_is_current(registry, deployment_service, unit_id, commit_sha):
                    continue
                result = deployment_service.submit(
                    DeploymentSubmissionRequest(unit_id=unit_id, branch="main", commit_sha=commit_sha)
                )
                session.commit()
                if result.status != "healthy":
                    raise RuntimeError(f"failed to bootstrap {unit_id}: {result.failure_reason or result.status}")

    def _deployment_is_current(
        self,
        registry: RegistryService,
        deployment_service: DeploymentService,
        unit_id: str,
        commit_sha: str,
    ) -> bool:
        unit = registry.get_unit(unit_id)
        if unit is None or not unit.active_deployment_id:
            return False

        deployment = registry.get_deployment(unit.active_deployment_id)
        if deployment is None or deployment.status != "healthy" or deployment.commit_sha != commit_sha:
            return False
        if self.settings.runtime_strategy != "docker":
            return True

        if not deployment.runtime_ref:
            return False
        try:
            runtime_ref = RuntimeRef.model_validate(deployment.runtime_ref)
        except Exception:
            return False
        health_url = build_runtime_health_url(runtime_ref)
        manifest = deployment_service._load_manifest(commit_sha, unit_id)
        return self._healthcheck_passes(manifest.health.path, health_url)

    @staticmethod
    def _healthcheck_passes(health_path: str, health_url: str) -> bool:
        if not health_url.endswith(health_path):
            return False
        try:
            response = httpx.get(health_url, timeout=5.0)
        except Exception:
            return False
        return response.status_code < 400


class ApplicationRegistryBootstrapper:
    """Seed built-in platform applications into the control-plane registry."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def ensure_seeded(self) -> None:
        session_factory = get_session_factory()
        with session_factory() as session:
            registry = RegistryService(session)
            registry.seed_builtin_applications(self.settings.resolved_apps_source_root)
            session.commit()
