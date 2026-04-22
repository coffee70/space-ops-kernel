"""Control-plane configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field
from pydantic_settings import BaseSettings


def default_workspace_root() -> Path:
    """Resolve a sane workspace root in local and containerized layouts."""

    config_path = Path(__file__).resolve()
    if len(config_path.parents) > 3 and config_path.parents[2].name == "space-ops-kernel":
        return config_path.parents[3]
    if len(config_path.parents) > 1:
        return config_path.parents[1]
    return config_path.parent


class Settings(BaseSettings):
    """Settings loaded from environment variables."""

    model_config = ConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Space Ops Kernel Control Plane"
    environment: str = "development"
    control_plane_host: str = "0.0.0.0"
    control_plane_port: int = 8100
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    runtime_strategy: Literal["stub", "docker"] = "stub"
    deployment_command_timeout_seconds: int = 300
    deployment_health_timeout_seconds: int = 45
    deployment_health_poll_interval_seconds: float = 1.5
    database_url: str | None = None
    workspace_root: Path = Field(default_factory=default_workspace_root)
    runtime_root: Path | None = None
    platform_source_root: Path | None = None
    apps_source_root: Path | None = None
    compose_file: Path | None = None
    compose_project_name: str = "space-ops-kernel"

    @property
    def kernel_root(self) -> Path:
        return self.workspace_root / "space-ops-kernel"

    @property
    def resolved_runtime_root(self) -> Path:
        return self.runtime_root or (self.kernel_root / "runtime")

    @property
    def managed_fork_root(self) -> Path:
        return self.resolved_runtime_root / "managed-fork"

    @property
    def bare_repo_dir(self) -> Path:
        return self.managed_fork_root / "repo.git"

    @property
    def worktrees_root(self) -> Path:
        return self.managed_fork_root / "worktrees"

    @property
    def main_worktree_dir(self) -> Path:
        return self.worktrees_root / "main"

    @property
    def branch_worktrees_dir(self) -> Path:
        return self.worktrees_root / "branches"

    @property
    def deployment_workspaces_root(self) -> Path:
        return self.resolved_runtime_root / "deployment-workspaces"

    @property
    def artifacts_root(self) -> Path:
        return self.resolved_runtime_root / "artifacts"

    @property
    def generated_root(self) -> Path:
        return self.resolved_runtime_root / "generated"

    @property
    def generated_compose_root(self) -> Path:
        return self.generated_root / "compose"

    @property
    def generated_env_root(self) -> Path:
        return self.generated_root / "env"

    @property
    def deployment_logs_root(self) -> Path:
        return self.resolved_runtime_root / "deployment-logs"

    @property
    def resolved_platform_source_root(self) -> Path:
        return self.platform_source_root or (self.workspace_root / "space-ops-platform")

    @property
    def resolved_apps_source_root(self) -> Path:
        return self.apps_source_root or (self.workspace_root / "space-ops-apps")

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.resolved_runtime_root / 'control-plane.db').as_posix()}"

    @property
    def resolved_compose_file(self) -> Path:
        return self.compose_file or (self.kernel_root / "docker-compose.yml")

    @property
    def allowed_code_roots(self) -> tuple[str, ...]:
        return (
            "project/space-ops-platform",
            "project/space-ops-apps",
            "manifests/units",
        )

    def get_cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.resolved_runtime_root,
            self.managed_fork_root,
            self.worktrees_root,
            self.branch_worktrees_dir,
            self.deployment_workspaces_root,
            self.artifacts_root,
            self.generated_compose_root,
            self.generated_env_root,
            self.deployment_logs_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Return cached settings."""

    return Settings()
