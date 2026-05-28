"""Control-plane configuration."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator
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
    deployment_worker_poll_interval_seconds: float = 2.0
    deployment_worker_stale_after_minutes: int = 20
    runtime_bootstrap_max_parallel_deployments: int = 3
    runtime_proxy_connect_timeout_seconds: float = 2.0
    runtime_proxy_read_timeout_seconds: float = 8.0
    proxy_allowed_schemes: tuple[str, ...] = ("http",)
    proxy_allow_ip_hosts: bool = False
    proxy_allow_localhost_hosts: bool = False
    proxy_allowed_host_suffixes: tuple[str, ...] = ()
    proxy_allowed_hosts: tuple[str, ...] = ()
    database_url: str = Field(..., min_length=1)
    database_pool_size: int = 10
    database_max_overflow: int = 30
    platform_database_url: str = "postgresql://telemetry:telemetry@postgres:5432/telemetry_db"
    platform_openai_api_key: str = ""
    platform_anthropic_api_key: str = ""
    platform_openai_base_url: str = ""
    platform_agent_runtime_log_stream_parts: str = "false"
    platform_agent_runtime_max_steps: str = "10"
    platform_agent_runtime_request_timeout_ms: str = "240000"
    platform_api_base_url: str = "http://platform-api:8000"
    platform_control_plane_url: str = "http://control-plane:8100"
    platform_nats_url: str = "nats://nats:4222"
    platform_vehicle_config_root: str = "/app/platform/backend/resources/vehicle-configurations"
    platform_persistent_vehicle_config_root: str = "/app/vehicle-configurations"
    platform_models_registry_container_dir: str = "/app/model-registry"
    platform_models_registry_filename: str = "models.local.yaml"
    platform_satnogs_api_token: str = ""
    platform_satnogs_live_enabled: str = "false"
    platform_satnogs_adapter_config: str = "app/adapters/satnogs/config.example.yaml"
    platform_satnogs_dlq_root: str = "/app/runtime/satnogs-adapter/dlq"
    frontend_api_server_url: str = "http://telemetry-platform-edge-proxy:8080"
    frontend_control_plane_server_url: str = "http://control-plane:8100"
    frontend_next_public_api_url: str = ""
    frontend_next_public_control_plane_url: str = ""
    # Controls how Layer 1 materializes frontend_shell runtime units.
    # production: build artifact + production server command from manifest.
    # development: live source mount + Next dev server command for debugging through the real 8080 stack.
    frontend_shell_runtime_mode: Literal["production", "development"] = "production"
    workspace_root: Path = Field(default_factory=default_workspace_root)
    docker_host_workspace_root: Path | None = None
    runtime_root: Path | None = None
    platform_source_root: Path | None = None
    apps_source_root: Path | None = None
    compose_file: Path | None = None
    compose_project_name: str = "space-ops-kernel"

    @field_validator(
        "docker_host_workspace_root",
        "runtime_root",
        "platform_source_root",
        "apps_source_root",
        "compose_file",
        mode="before",
    )
    @classmethod
    def _empty_path_is_none(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
    def bootstrap_seed_state_path(self) -> Path:
        return self.generated_root / "bootstrap-seed-state.json"

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
    def resolved_vehicle_config_root(self) -> Path:
        return self.resolved_apps_source_root / "vehicle-configurations"

    @property
    def platform_models_local_yaml_container_path(self) -> str:
        """Container path used by model-registry-service MODEL_CONFIG_PATH."""

        base = self.platform_models_registry_container_dir.rstrip("/")
        return f"{base}/{self.platform_models_registry_filename}"

    @property
    def resolved_database_url(self) -> str:
        return self.database_url

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
