"""Deployment orchestration."""

from __future__ import annotations

import os
import shutil
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


def _decode_mountinfo_path(value: str) -> Path:
    """Decode the small set of mountinfo escapes relevant to path fields."""

    return Path(
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _docker_desktop_host_path(source: str, root: str) -> Path | None:
    """Translate Docker Desktop's /run/host_mark source notation to a host path."""

    marker = "/run/host_mark"
    if not source.startswith(marker):
        return None
    source_without_marker = source[len(marker) :]
    if "[" not in source_without_marker or not source_without_marker.endswith("]"):
        host_prefix = _decode_mountinfo_path(source_without_marker)
        host_root = _decode_mountinfo_path(root).relative_to("/")
        return (host_prefix / host_root).resolve()
    prefix, suffix = source_without_marker.split("[", 1)
    return _decode_mountinfo_path(f"{prefix}{suffix[:-1]}")


class DeploymentService:
    """Turn managed fork commits into running capabilities."""

    def __init__(self, settings: Settings, repository: ManagedGitRepository, session: Session):
        self.settings = settings
        self.repository = repository
        self.session = session
        self.registry = RegistryService(session)

    def submit(self, request: DeploymentSubmissionRequest, *, delete_eligible: bool = True) -> DeploymentRecordResponse:
        branch = request.branch
        commit_sha = self.repository.resolve_commit(branch, request.commit_sha)
        deployment = self.registry.create_deployment(request.unit_id, branch, commit_sha, delete_eligible=delete_eligible)
        logs_path = self.settings.deployment_logs_root / f"{deployment.deployment_id}.log"
        logs_path.parent.mkdir(parents=True, exist_ok=True)

        workspace = self.settings.deployment_workspaces_root / deployment.deployment_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        try:
            previous_runtime_ref = self.registry.get_runtime_ref_for_unit(request.unit_id)
        except Exception:
            previous_runtime_ref = None

        try:
            manifest = self._load_manifest(commit_sha, request.unit_id)
            self.registry.mark_build_started(deployment)
            source_root = workspace / "source"
            self.repository.materialize_commit(commit_sha, source_root)
            artifact_root = self.settings.artifacts_root / deployment.deployment_id
            artifact_root.mkdir(parents=True, exist_ok=True)
            self.registry.mark_build_finished(deployment, artifact_ref=str(artifact_root))

            runtime_ref = self._deploy_runtime(
                deployment_id=deployment.deployment_id,
                manifest=manifest,
                source_root=source_root,
                logs_path=logs_path,
            )
            self._run_health_check(runtime_ref)
            self.registry.register_healthy_deployment(
                deployment,
                manifest,
                runtime_ref.model_dump(mode="json"),
            )

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
            result = run_command(command, timeout=self.settings.deployment_command_timeout_seconds)
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
                    "OPENAI_BASE_URL": self.settings.platform_openai_base_url,
                    "PLATFORM_API_BASE_URL": self.settings.platform_api_base_url,
                    "VEHICLE_CONFIG_ROOT": self.settings.platform_vehicle_config_root,
                    "CONTROL_PLANE_URL": self.settings.platform_control_plane_url,
                    "NATS_URL": self.settings.platform_nats_url,
                }
            )
            shared_models_path = self.settings.platform_models_local_yaml_container_path
            if manifest.unit_id == "model-config-service":
                env["MODEL_CONFIG_PATH"] = shared_models_path
                cp_base = self.settings.platform_control_plane_url.rstrip("/")
                env["AGENT_RUNTIME_BASE_URL"] = f"{cp_base}/internal/runtime-services/agent-runtime-service"
            if manifest.unit_id == "agent-runtime-service":
                env["AGENT_RUNTIME_MODELS_CONFIG_PATH"] = shared_models_path
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
        volume_entries = self._compose_volume_entries(manifest)
        if volume_entries:
            service_spec["volumes"] = volume_entries

        payload = {"services": {service_name: service_spec}}
        return payload

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
        """Return a Docker-daemon-visible host path for workspace_root when known.

        The control-plane often runs inside a container while talking to the host Docker
        daemon. Compose can tar build contexts from the container, but bind mount
        sources must be paths the host daemon can access.
        """

        configured = self.settings.docker_host_workspace_root
        if configured is not None:
            return configured.resolve()

        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return None

        best_target: Path | None = None
        best_source: Path | None = None
        for line in mountinfo.splitlines():
            try:
                left, right = line.split(" - ", 1)
                fields = left.split()
                mount_root = fields[3]
                mount_point = _decode_mountinfo_path(fields[4]).resolve()
                source_field = right.split()[1]
            except (IndexError, ValueError):
                continue
            try:
                workspace_root.relative_to(mount_point)
            except ValueError:
                continue
            source_path = _docker_desktop_host_path(source_field, mount_root) or _decode_mountinfo_path(
                source_field
            )
            if best_target is None or len(mount_point.parts) > len(best_target.parts):
                best_target = mount_point
                best_source = source_path

        if best_target is None or best_source is None:
            return None
        return (best_source / workspace_root.relative_to(best_target)).resolve()

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
            status=deployment.status,
            health_status=deployment.health_status,
            logs_url=f"/deployments/{deployment.deployment_id}/logs",
            registered=deployment.status == "healthy",
            failure_reason=deployment.failure_reason,
        )
