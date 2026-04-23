"""Deployment orchestration."""

from __future__ import annotations

import shutil
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


class DeploymentService:
    """Turn managed fork commits into running capabilities."""

    def __init__(self, settings: Settings, repository: ManagedGitRepository, session: Session):
        self.settings = settings
        self.repository = repository
        self.session = session
        self.registry = RegistryService(session)

    def submit(self, request: DeploymentSubmissionRequest) -> DeploymentRecordResponse:
        branch = request.branch
        commit_sha = self.repository.resolve_commit(branch, request.commit_sha)
        deployment = self.registry.create_deployment(request.unit_id, branch, commit_sha)
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
        temp_root = self.settings.deployment_workspaces_root / f"manifest-{unit_id}"
        if temp_root.exists():
            shutil.rmtree(temp_root)
        temp_root.mkdir(parents=True, exist_ok=True)
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
        if manifest.unit_kind == "module":
            route_slug = manifest.discovery.get("route_slug", manifest.unit_id)
            return f"/runtime-modules/{route_slug}"
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
                    "VEHICLE_CONFIG_ROOT": "/app/vehicle-configurations",
                    "CONTROL_PLANE_URL": self.settings.platform_control_plane_url,
                    "NATS_URL": self.settings.platform_nats_url,
                }
            )
        if manifest.unit_kind == "module":
            route_slug = manifest.discovery.get("route_slug", manifest.unit_id)
            env["MODULE_BASE_PATH"] = f"/runtime-modules/{route_slug}"
            env["MODULE_ROUTE_SLUG"] = route_slug
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

        payload = {
            "services": {
                service_name: {
                    "build": {
                        "context": str(self._build_context_path(manifest, source_root)),
                        "dockerfile": self._build_dockerfile_path(manifest),
                    },
                    "command": manifest.run.command,
                    "environment": self._build_runtime_env(manifest, service_name),
                }
            }
        }
        return payload

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
