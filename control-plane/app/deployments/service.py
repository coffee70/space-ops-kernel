"""Deployment orchestration."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy.orm import Session

from app.config import Settings
from app.git.repository import ManagedGitRepository
from app.registry.service import RegistryService
from app.schemas import (
    DeploymentRecordResponse,
    DeploymentSubmissionRequest,
    RuntimeHealth,
    RuntimeProxy,
    RuntimeRef,
    RuntimeTransport,
    UnitManifest,
)
from app.services.proxy_targets import build_runtime_health_url
from app.services.shell import run_command

PERSISTENT_VEHICLE_CONFIG_READERS = {
    "vehicle-config-service",
    "simulator-service",
    "simulator-2-service",
    "satnogs-adapter-service",
}

DEPLOYMENT_COMMAND_OUTPUT_TAIL_CHARS = 64_000


def _compose_safe_run_command(run_command: str) -> Any:
    """Return command as a Compose exec list for common `sh -c "..."` manifests.

    YAML safe_dump folds long quoted strings awkwardly (newlines inside sh -c), which breaks deployments.
    """
    trimmed = run_command.strip()
    prefix = 'sh -c "'
    if trimmed.startswith(prefix) and trimmed.endswith('"') and len(trimmed) >= len(prefix) + 2:
        inner_script = trimmed[len(prefix) : -1]
        return ["sh", "-c", inner_script]
    return run_command


class DeploymentService:
    """Turn managed fork commits into running capabilities."""

    def __init__(self, settings: Settings, repository: ManagedGitRepository, session: Session):
        self.settings = settings
        self.repository = repository
        self.session = session
        self.registry = RegistryService(session)

    def _is_frontend_shell_development_runtime(self, manifest: UnitManifest) -> bool:
        return (
            manifest.runtime_kind == "frontend_shell"
            and self.settings.frontend_shell_runtime_mode == "development"
        )

    def submit(self, request: DeploymentSubmissionRequest, *, delete_eligible: bool = True) -> DeploymentRecordResponse:
        return self.enqueue_deployment(request, delete_eligible=delete_eligible)

    def enqueue_deployment(
        self,
        request: DeploymentSubmissionRequest,
        *,
        delete_eligible: bool = True,
    ) -> DeploymentRecordResponse:
        branch = request.branch
        commit_sha = self.repository.resolve_commit(branch, request.commit_sha)
        in_progress = self.registry.get_in_progress_deployment_for_unit(request.unit_id)
        if in_progress is not None:
            return self._to_response(in_progress.deployment_id)
        deployment = self.registry.create_deployment(
            request.unit_id,
            branch,
            commit_sha,
            deployment_intent=request.deployment_intent,
            delete_eligible=delete_eligible,
        )
        logs_path = self.settings.deployment_logs_root / f"{deployment.deployment_id}.log"
        logs_path.parent.mkdir(parents=True, exist_ok=True)
        self._append_log(
            logs_path,
            "\n".join(
                [
                    "Deployment queued",
                    f"deployment_id: {deployment.deployment_id}",
                    f"unit_id: {deployment.unit_id}",
                    f"branch: {deployment.branch}",
                    f"commit_sha: {deployment.commit_sha}",
                    "",
                ]
            ),
        )
        self.session.flush()
        return self._to_response(deployment.deployment_id)

    def execute_deployment(self, deployment_id: str) -> DeploymentRecordResponse:
        deployment = self.registry.get_deployment(deployment_id)
        if deployment is None:
            raise LookupError(deployment_id)
        if deployment.status not in {"queued", "materializing", "building", "health_checking"}:
            return self._to_response(deployment.deployment_id)

        logs_path = self.settings.deployment_logs_root / f"{deployment.deployment_id}.log"
        logs_path.parent.mkdir(parents=True, exist_ok=True)
        workspace = self.settings.deployment_workspaces_root / deployment.deployment_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        try:
            previous_runtime_ref = self.registry.get_runtime_ref_for_unit(deployment.unit_id)
        except Exception:
            previous_runtime_ref = None

        try:
            if deployment.status == "queued":
                self.registry.mark_materializing(deployment)
            self._append_log(logs_path, "Deployment claimed by worker\n")
            self._append_log(logs_path, "Materializing source\n")
            manifest = self._load_manifest(deployment.commit_sha, deployment.unit_id)
            self.registry.mark_build_started(deployment)
            source_root = workspace / "source"
            self.repository.materialize_commit(deployment.commit_sha, source_root)
            artifact_root = self.settings.artifacts_root / deployment.deployment_id
            artifact_root.mkdir(parents=True, exist_ok=True)
            self.registry.mark_build_finished(deployment, artifact_ref=str(artifact_root))

            self._append_log(logs_path, "Generating compose fragment\n")
            runtime_ref = self._deploy_runtime(
                deployment_id=deployment.deployment_id,
                manifest=manifest,
                source_root=source_root,
                logs_path=logs_path,
            )
            self.registry.mark_health_checking(deployment)
            self._append_log(logs_path, "Starting health checks\n")
            self._run_health_check(runtime_ref)
            self.registry.register_healthy_deployment(
                deployment,
                manifest,
                runtime_ref.model_dump(mode="json"),
            )
            self._append_log(logs_path, "Deployment healthy\n")

            if previous_runtime_ref and self.settings.runtime_strategy == "docker":
                self._teardown_runtime(previous_runtime_ref, logs_path)

            self.session.flush()
            return self._to_response(deployment.deployment_id)
        except Exception as exc:
            self._append_log(logs_path, f"DEPLOYMENT FAILED: {exc}\n")
            self.registry.mark_failed(deployment, str(exc))
            self.session.flush()
            return self._to_response(deployment.deployment_id)

    def _load_manifest(self, commit_sha: str, unit_id: str) -> UnitManifest:
        self.settings.deployment_workspaces_root.mkdir(parents=True, exist_ok=True)
        temp_root = Path(
            tempfile.mkdtemp(
                prefix=f"manifest-{unit_id}-",
                dir=self.settings.deployment_workspaces_root,
            )
        )
        try:
            self.repository.materialize_commit(commit_sha, temp_root)
            manifest_path = temp_root / "manifests" / "units" / f"{unit_id}.yaml"
            if not manifest_path.exists():
                raise FileNotFoundError(f"manifest not found for {unit_id}")
            payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            manifest = UnitManifest.model_validate(payload)
            return manifest
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _deploy_runtime(
        self,
        *,
        deployment_id: str,
        manifest: UnitManifest,
        source_root: Path,
        logs_path: Path,
    ) -> RuntimeRef:
        service_name = f"{manifest.unit_id}-{deployment_id}".replace("_", "-")
        compose_path = self.settings.generated_compose_root / f"{deployment_id}.yaml"
        env_path = self.settings.generated_env_root / f"{deployment_id}.env"
        env_values = self._build_runtime_env(manifest, service_name)
        env_path.write_text(
            "".join(f"{key}={value}\n" for key, value in env_values.items()),
            encoding="utf-8",
        )

        if self._is_frontend_shell_development_runtime(manifest):
            self._append_log(
                logs_path,
                "Frontend shell runtime mode: development - using Dockerfile.dev, npm run dev, and live source mount\n",
            )
        elif manifest.runtime_kind == "frontend_shell":
            self._append_log(
                logs_path,
                "Frontend shell runtime mode: production - using manifest build/run settings\n",
            )

        compose_payload = self._build_compose_payload(
            manifest=manifest,
            source_root=source_root,
            service_name=service_name,
            env_path=env_path,
        )
        compose_path.write_text(yaml.safe_dump(compose_payload, sort_keys=False), encoding="utf-8")
        self._append_log(logs_path, f"Compose fragment: {compose_path}\n")

        runtime_ref = self._build_runtime_ref(
            manifest=manifest,
            service_name=service_name,
            compose_path=compose_path,
            env_path=env_path,
        )

        if self.settings.runtime_strategy == "docker":
            command = [
                *self._compose_command(),
                "-f",
                str(self.settings.resolved_compose_file),
                "-f",
                str(compose_path),
                "up",
                "-d",
                "--build",
                service_name,
            ]
            self._append_log(logs_path, "Running docker compose up -d --build\n")
            self._append_log(logs_path, f"Command: {shlex.join(command)}\n")
            try:
                result = run_command(command, timeout=self.settings.deployment_command_timeout_seconds)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                self._append_command_failure_logs(logs_path, command, exc)
                raise
            self._append_log(logs_path, result.stdout)
            self._append_log(logs_path, result.stderr)
        else:
            self._append_log(logs_path, "Stub runtime strategy enabled; skipping docker compose.\n")
        return runtime_ref

    def _build_runtime_ref(
        self,
        *,
        manifest: UnitManifest,
        service_name: str,
        compose_path: Path,
        env_path: Path,
    ) -> RuntimeRef:
        return RuntimeRef(
            service_name=service_name,
            compose_file=str(compose_path),
            env_file=str(env_path),
            transport=RuntimeTransport(
                scheme="http",
                host=service_name,
                port=manifest.health.port,
            ),
            health=RuntimeHealth(path=self._build_health_path(manifest)),
            proxy=RuntimeProxy(base_path=self._build_proxy_base_path(manifest)),
        )

    @staticmethod
    def _build_health_path(manifest: UnitManifest) -> str:
        return manifest.health.path

    @staticmethod
    def _build_proxy_base_path(manifest: UnitManifest) -> str:
        if manifest.runtime_kind == "frontend_application" and manifest.application:
            return manifest.application.proxy_base_path or ""
        return ""

    def _run_health_check(self, runtime_ref: RuntimeRef) -> None:
        if self.settings.runtime_strategy == "stub":
            return
        health_url = build_runtime_health_url(runtime_ref)
        with httpx.Client(timeout=5.0) as client:
            attempts = max(
                1,
                int(
                    self.settings.deployment_health_timeout_seconds
                    / self.settings.deployment_health_poll_interval_seconds
                ),
            )
            last_error: str | None = None
            for _ in range(attempts):
                try:
                    response = client.get(health_url)
                    if response.status_code < 400:
                        return
                    last_error = f"health check returned {response.status_code}"
                except Exception as exc:
                    last_error = str(exc)
                import time

                time.sleep(self.settings.deployment_health_poll_interval_seconds)
            raise RuntimeError(last_error or "health check failed")

    def _teardown_runtime(self, runtime_ref: RuntimeRef, logs_path: Path) -> None:
        compose_file = runtime_ref.compose_file
        service_name = runtime_ref.service_name
        if not compose_file or not service_name:
            return
        result = run_command(
            [
                *self._compose_command(),
                "-f",
                str(self.settings.resolved_compose_file),
                "-f",
                str(compose_file),
                "rm",
                "-sf",
                service_name,
            ],
            timeout=self.settings.deployment_command_timeout_seconds,
        )
        self._append_log(logs_path, result.stdout)
        self._append_log(logs_path, result.stderr)

    def _build_runtime_env(self, manifest: UnitManifest, service_name: str) -> dict[str, str]:
        env = {
            "UNIT_ID": manifest.unit_id,
            "DISPLAY_NAME": manifest.display_name,
            "PORT": str(manifest.health.port),
        }
        if manifest.package_owner == "space-ops-platform":
            env.update(
                {
                    "DATABASE_URL": self.settings.platform_database_url,
                    "OPENAI_API_KEY": self.settings.platform_openai_api_key,
                    "ANTHROPIC_API_KEY": self.settings.platform_anthropic_api_key,
                    "OPENAI_BASE_URL": self.settings.platform_openai_base_url,
                    "PLATFORM_API_BASE_URL": self.settings.platform_api_base_url,
                    "VEHICLE_CONFIG_ROOT": self.settings.platform_vehicle_config_root,
                    "CONTROL_PLANE_URL": self.settings.platform_control_plane_url,
                    "NATS_URL": self.settings.platform_nats_url,
                }
            )
            if manifest.unit_id in PERSISTENT_VEHICLE_CONFIG_READERS:
                env["VEHICLE_CONFIG_ROOT"] = self.settings.platform_persistent_vehicle_config_root
            shared_models_path = self.settings.platform_models_local_yaml_container_path
            if manifest.unit_id == "model-registry-service":
                env["MODEL_CONFIG_PATH"] = shared_models_path
            if manifest.unit_id == "agent-runtime-service":
                cp_base = self.settings.platform_control_plane_url.rstrip("/")
                env["MODEL_REGISTRY_BASE_URL"] = f"{cp_base}/internal/runtime-services/model-registry-service"
                env["AGENT_RUNTIME_LOG_STREAM_PARTS"] = self.settings.platform_agent_runtime_log_stream_parts
                env["AGENT_RUNTIME_MAX_STEPS"] = self.settings.platform_agent_runtime_max_steps
                env["AGENT_RUNTIME_REQUEST_TIMEOUT_MS"] = self.settings.platform_agent_runtime_request_timeout_ms
            if manifest.unit_id == "satnogs-adapter-service":
                env.update(
                    {
                        "SATNOGS_API_TOKEN": self.settings.platform_satnogs_api_token,
                        "SATNOGS_LIVE_ENABLED": self.settings.platform_satnogs_live_enabled,
                        "SATNOGS_ADAPTER_CONFIG": self.settings.platform_satnogs_adapter_config,
                        "SATNOGS_DLQ_ROOT": self.settings.platform_satnogs_dlq_root,
                    }
                )
            if manifest.unit_id in {"simulator-service", "simulator-2-service"}:
                env["BACKEND_URL"] = self.settings.platform_api_base_url
                if manifest.unit_id == "simulator-service":
                    env["VEHICLE_CONFIG_PATH"] = "simulators/drogonsat.yaml"
                elif manifest.unit_id == "simulator-2-service":
                    env["VEHICLE_CONFIG_PATH"] = "simulators/rhaegalsat.json"
        if manifest.runtime_kind in {"frontend_application", "frontend_shell"}:
            env.update(
                {
                    "API_SERVER_URL": self.settings.frontend_api_server_url,
                    "CONTROL_PLANE_SERVER_URL": self.settings.frontend_control_plane_server_url,
                    "NEXT_PUBLIC_API_URL": self.settings.frontend_next_public_api_url,
                    "NEXT_PUBLIC_CONTROL_PLANE_URL": self.settings.frontend_next_public_control_plane_url,
                }
            )
            if self._is_frontend_shell_development_runtime(manifest):
                env.update(
                    {
                        "NODE_ENV": "development",
                        "NEXT_TELEMETRY_DISABLED": "1",
                        "WATCHPACK_POLLING": "true",
                        "CHOKIDAR_USEPOLLING": "true",
                    }
                )
        if manifest.runtime_kind == "frontend_application" and manifest.application:
            env["APPLICATION_ID"] = manifest.application.application_id
            if manifest.application.proxy_base_path:
                env["APPLICATION_PROXY_BASE_PATH"] = manifest.application.proxy_base_path
            env["SERVICE_NAME"] = service_name
        return env

    def _build_compose_payload(
        self,
        *,
        manifest: UnitManifest,
        source_root: Path,
        service_name: str,
        env_path: Path,
    ) -> dict[str, Any]:
        unit_source_root = source_root / manifest.source_path
        if not unit_source_root.exists():
            raise FileNotFoundError(f"deployment source path not found: {manifest.source_path}")
        try:
            unit_source_root.relative_to(source_root)
        except ValueError as exc:
            raise ValueError(f"deployment source path escapes exported commit: {manifest.source_path}") from exc

        service_spec: dict[str, Any] = {
            "build": {
                "context": str(self._build_context_path(manifest, source_root)),
                "dockerfile": self._build_dockerfile_path(manifest),
            },
            "command": _compose_safe_run_command(manifest.run.command),
            "environment": self._build_runtime_env(manifest, service_name),
        }
        if self._is_frontend_shell_development_runtime(manifest):
            service_spec["build"] = {
                "context": str(self._build_context_path(manifest, source_root)),
                "dockerfile": "Dockerfile.dev",
            }
            service_spec["command"] = [
                "npm",
                "run",
                "dev",
                "--",
                "--hostname",
                "0.0.0.0",
                "--port",
                str(manifest.health.port),
            ]

        bind_volume_entries = self._compose_volume_entries(manifest)
        named_volume_entries, named_volume_declarations = self._compose_named_volume_entries(manifest)
        frontend_dev_volume_entries: list[str] = []
        if self._is_frontend_shell_development_runtime(manifest):
            frontend_dev_volume_entries, frontend_dev_volume_declarations = self._frontend_shell_development_volume_entries(
                manifest,
                service_name,
            )
            named_volume_declarations.update(frontend_dev_volume_declarations)

        service_volumes = [*bind_volume_entries, *named_volume_entries, *frontend_dev_volume_entries]
        if service_volumes:
            service_spec["volumes"] = service_volumes

        payload = {"services": {service_name: service_spec}}
        if named_volume_declarations:
            payload["volumes"] = named_volume_declarations
        return payload

    def _compose_named_volume_entries(self, manifest: UnitManifest) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """Mount Docker-managed named volumes and declare top-level Compose volume keys."""

        entries: list[str] = []
        declarations: dict[str, dict[str, Any]] = {}
        for volume in manifest.named_volumes:
            mode = "ro" if volume.read_only else "rw"
            entries.append(f"{volume.name}:{volume.target}:{mode}")
            declarations[volume.name] = {}
        return entries, declarations

    def _frontend_shell_development_volume_entries(
        self,
        manifest: UnitManifest,
        service_name: str,
    ) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """Return dev-mode volumes for the managed frontend shell.

        The deployment worker runs inside a container, but generated Docker Compose
        bind mounts are resolved by the Docker daemon host. Validate the source via
        the deployment-worker-visible apps source root, then emit a host-visible path
        when DOCKER_HOST_WORKSPACE_ROOT is configured.
        """

        unit_source_path = Path(manifest.source_path)
        try:
            relative_apps_path = unit_source_path.relative_to("project/space-ops-apps")
        except ValueError as exc:
            raise ValueError(
                "frontend_shell development runtime requires source_path under "
                f"project/space-ops-apps, got: {manifest.source_path}"
            ) from exc

        container_visible_source = (self.settings.resolved_apps_source_root / relative_apps_path).resolve()
        if not container_visible_source.is_dir():
            raise FileNotFoundError(f"frontend shell dev source path not found: {container_visible_source}")

        if self.settings.docker_host_workspace_root is not None:
            compose_visible_source = (
                self.settings.docker_host_workspace_root.resolve()
                / "space-ops-apps"
                / relative_apps_path
            )
        else:
            compose_visible_source = container_visible_source
        node_modules_volume = f"{service_name}-node-modules"
        next_cache_volume = f"{service_name}-next-cache"

        entries = [
            f"{compose_visible_source}:/app:rw",
            f"{node_modules_volume}:/app/node_modules:rw",
            f"{next_cache_volume}:/app/.next:rw",
        ]
        declarations = {
            node_modules_volume: {},
            next_cache_volume: {},
        }
        return entries, declarations

    def _compose_volume_entries(self, manifest: UnitManifest) -> list[str]:
        """Bind-mount declared host paths into the service (paths relative to generated compose dir)."""

        workspace_root = self.settings.workspace_root.resolve()
        docker_host_workspace_root = self._docker_host_workspace_root(workspace_root)
        compose_dir = self.settings.generated_compose_root
        compose_dir.mkdir(parents=True, exist_ok=True)
        compose_resolved = compose_dir.resolve()

        entries: list[str] = []
        for mount in manifest.mounts:
            host_path = (workspace_root / mount.source).resolve()
            try:
                host_path.relative_to(workspace_root)
            except ValueError as exc:
                raise ValueError(f"mount source resolves outside workspace_root: {mount.source}") from exc
            if not host_path.is_dir():
                continue
            if docker_host_workspace_root is None:
                compose_host = Path(os.path.relpath(str(host_path), str(compose_resolved))).as_posix()
            else:
                mount_relpath = host_path.relative_to(workspace_root)
                compose_host = str((docker_host_workspace_root / mount_relpath).resolve())
            mode = "ro" if mount.read_only else "rw"
            entries.append(f"{compose_host}:{mount.target}:{mode}")
        return entries

    def _docker_host_workspace_root(self, workspace_root: Path) -> Path | None:
        """Return an explicitly configured Docker-daemon-visible workspace path."""

        _ = workspace_root
        configured = self.settings.docker_host_workspace_root
        if configured is not None:
            return configured.resolve()
        return None

    def _build_context_path(self, manifest: UnitManifest, source_root: Path) -> Path:
        if manifest.package_owner == "space-ops-platform" and manifest.source_path == "project/space-ops-platform":
            return source_root / "project"
        return source_root / manifest.source_path

    @staticmethod
    def _build_dockerfile_path(manifest: UnitManifest) -> str:
        if manifest.package_owner == "space-ops-platform" and manifest.source_path == "project/space-ops-platform":
            return "space-ops-platform/Dockerfile"
        return "Dockerfile"

    @staticmethod
    def _append_log(path: Path, content: str) -> None:
        if not content:
            return
        with path.open("a", encoding="utf-8") as handle:
            handle.write(content)

    @classmethod
    def _append_command_failure_logs(
        cls,
        logs_path: Path,
        command: list[str],
        exc: subprocess.CalledProcessError | subprocess.TimeoutExpired,
    ) -> None:
        cls._append_log(logs_path, "\nDeployment command failed\n")
        cls._append_log(logs_path, f"command: {shlex.join(command)}\n")
        if isinstance(exc, subprocess.CalledProcessError):
            cls._append_log(logs_path, f"exit_code: {exc.returncode}\n")
        else:
            cls._append_log(logs_path, f"timeout_seconds: {exc.timeout}\n")
        cls._append_output_tail(logs_path, "stdout", getattr(exc, "stdout", None))
        cls._append_output_tail(logs_path, "stderr", getattr(exc, "stderr", None))

    @classmethod
    def _append_output_tail(cls, logs_path: Path, label: str, output: str | bytes | None) -> None:
        text = cls._coerce_output_text(output)
        if not text:
            return
        truncated = len(text) > DEPLOYMENT_COMMAND_OUTPUT_TAIL_CHARS
        tail = text[-DEPLOYMENT_COMMAND_OUTPUT_TAIL_CHARS:] if truncated else text
        if truncated:
            cls._append_log(logs_path, f"\n--- {label} tail (last {DEPLOYMENT_COMMAND_OUTPUT_TAIL_CHARS} chars) ---\n")
        else:
            cls._append_log(logs_path, f"\n--- {label} ---\n")
        cls._append_log(logs_path, tail)
        if not tail.endswith("\n"):
            cls._append_log(logs_path, "\n")

    @staticmethod
    def _coerce_output_text(output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            return output.decode("utf-8", errors="replace")
        return output

    def _compose_command(self) -> list[str]:
        docker_compose = shutil.which("docker-compose")
        if docker_compose:
            return [docker_compose, "-p", self.settings.compose_project_name]
        docker_cli = shutil.which("docker")
        if docker_cli:
            return [docker_cli, "compose", "-p", self.settings.compose_project_name]
        raise FileNotFoundError("docker compose client not found")

    def _to_response(self, deployment_id: str) -> DeploymentRecordResponse:
        deployment = self.registry.get_deployment(deployment_id)
        if deployment is None:
            raise LookupError(deployment_id)
        return DeploymentRecordResponse(
            deployment_id=deployment.deployment_id,
            unit_id=deployment.unit_id,
            branch=deployment.branch,
            commit_sha=deployment.commit_sha,
            deployment_intent=deployment.deployment_intent,
            status=deployment.status,
            health_status=deployment.health_status,
            logs_url=f"/deployments/{deployment.deployment_id}/logs",
            registered=deployment.status == "healthy",
            failure_reason=deployment.failure_reason,
            validation_status=self.registry.summarize_validation_status(deployment.deployment_id),
            next_validation_steps=self.registry.suggested_validation_steps_for_deployment(deployment),
            success_claim_allowed=self.registry.success_claim_allowed(deployment),
        )
