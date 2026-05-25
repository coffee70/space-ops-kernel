"""Read-only deployment and service status aggregation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.runtime import Deployment, ManagedUnit
from app.registry.service import RegistryService
from app.schemas import (
    BootstrapSummary,
    DeploymentUiState,
    ServiceGroupSummary,
    ServiceStatusItem,
    SystemDeploymentOverviewResponse,
)
from app.services.bootstrap_service import BOOTSTRAP_UNITS
from app.services.bootstrap_status import RuntimeBootstrapStatusService

WARNING_STATES = {"deploying", "stale", "unknown", "skipped"}
BROKEN_STATES = {"missing", "failed", "crashed", "blocked"}
HEALTHY_STATES = {"healthy"}


@dataclass(frozen=True)
class ContainerStatus:
    """Small safe subset of Docker container status."""

    service: str
    name: str | None = None
    state: str | None = None
    status: str | None = None
    health: str | None = None
    exit_code: int | None = None


class SystemStatusService:
    """Builds the Control Panel deployments overview without mutating runtime state."""

    def __init__(self, settings: Settings, session: Session):
        self.settings = settings
        self.session = session
        self.registry = RegistryService(session)

    def build_overview(self) -> SystemDeploymentOverviewResponse:
        containers, docker_available = self.inspect_compose_containers()
        bootstrap_snapshot = RuntimeBootstrapStatusService().get_latest_run_snapshot()
        core = self._build_core_summary(containers)
        runtime = self._build_runtime_summary(containers, docker_available, bootstrap_snapshot)
        return SystemDeploymentOverviewResponse(
            generated_at=datetime.now(timezone.utc).isoformat(),
            overall_state=self._overall_state(core, runtime, bootstrap_snapshot),
            core=core,
            runtime=runtime,
            bootstrap=self._bootstrap_summary(bootstrap_snapshot),
        )

    def expected_core_services(self) -> list[str]:
        compose_file = self.settings.resolved_compose_file
        if not compose_file.is_file():
            return []
        data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
        services = data.get("services") if isinstance(data, dict) else None
        if not isinstance(services, dict):
            return []
        return [str(name) for name in services.keys()]

    def inspect_compose_containers(self) -> tuple[dict[str, ContainerStatus], bool]:
        compose_file = self.settings.resolved_compose_file
        project = self.settings.compose_project_name
        commands: list[list[str]] = []
        if compose_file.is_file():
            commands.append(["docker", "compose", "-p", project, "-f", str(compose_file), "ps", "-a", "--format", "json"])
        commands.append(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{json .}}",
            ]
        )

        docker_available = False
        containers: dict[str, ContainerStatus] = {}
        for command in commands:
            try:
                result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=5)
            except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
            docker_available = True
            containers = self._merge_container_statuses(containers, self._parse_container_output(result.stdout))
        return containers, docker_available

    def _merge_container_statuses(
        self,
        existing: dict[str, ContainerStatus],
        incoming: dict[str, ContainerStatus],
    ) -> dict[str, ContainerStatus]:
        merged = dict(existing)
        for service, incoming_status in incoming.items():
            current = merged.get(service)
            if current is None:
                merged[service] = incoming_status
                continue
            merged[service] = ContainerStatus(
                service=service,
                name=current.name or incoming_status.name,
                state=current.state or incoming_status.state,
                status=current.status or incoming_status.status,
                health=current.health or incoming_status.health,
                exit_code=current.exit_code if current.exit_code is not None else incoming_status.exit_code,
            )
        return merged

    def _build_core_summary(self, containers: dict[str, ContainerStatus]) -> ServiceGroupSummary:
        services = []
        for service_id in self.expected_core_services():
            container = containers.get(service_id)
            state = self._normalize_core_state(container)
            services.append(
                ServiceStatusItem(
                    id=service_id,
                    display_name=self._display_name(service_id),
                    group="core",
                    expected=True,
                    exists=container is not None,
                    ui_state=state,
                    health_status=container.health if container else None,
                    container_state=container.state if container else None,
                    container_status=container.status if container else None,
                    service_slug=service_id,
                    updated_at=None,
                    last_checked_at=datetime.now(timezone.utc).isoformat(),
                    failure_reason=self._container_failure(container) if state in BROKEN_STATES else None,
                    latest_error=self._container_failure(container) if state in BROKEN_STATES else None,
                    details={"container_name": container.name, "exit_code": container.exit_code} if container else {},
                )
            )
        return self._summarize(services)

    def _build_runtime_summary(
        self,
        containers: dict[str, ContainerStatus],
        docker_available: bool,
        bootstrap_snapshot: dict[str, Any],
    ) -> ServiceGroupSummary:
        units = {unit.unit_id: unit for unit in self.registry.get_units()}
        bootstrap_units = {
            unit.get("unit_id"): unit for unit in bootstrap_snapshot.get("units", []) if isinstance(unit, dict)
        }
        services: list[ServiceStatusItem] = []

        for unit_id in BOOTSTRAP_UNITS:
            unit = units.get(unit_id)
            active = self.registry.get_deployment(unit.active_deployment_id) if unit and unit.active_deployment_id else None
            latest = self.registry.get_latest_deployment_for_unit(unit_id)
            deployment = active or latest
            runtime_service = self._runtime_service_name(deployment)
            container = containers.get(runtime_service or "") or containers.get(unit_id)
            bootstrap_unit = bootstrap_units.get(unit_id) or {}
            state = self._normalize_runtime_state(unit, deployment, container, docker_available, bootstrap_unit)
            exists = self._runtime_exists(unit, deployment, container, docker_available)
            failure_reason = self._runtime_failure_reason(deployment, bootstrap_unit)
            latest_error = self._latest_runtime_error(deployment, failure_reason, container)

            services.append(
                ServiceStatusItem(
                    id=unit_id,
                    display_name=unit.display_name if unit else self._display_name(unit_id),
                    group="runtime",
                    expected=True,
                    exists=exists,
                    ui_state=state,
                    health_status=(deployment.health_status if deployment else unit.health_status if unit else None),
                    deployment_status=(deployment.status if deployment else unit.deployment_status if unit else None),
                    bootstrap_status=bootstrap_unit.get("status"),
                    container_state=container.state if container else None,
                    container_status=container.status if container else None,
                    active_deployment_id=unit.active_deployment_id if unit else None,
                    latest_deployment_id=latest.deployment_id if latest else None,
                    service_slug=runtime_service or unit_id,
                    runtime_kind=unit.runtime_kind if unit else None,
                    runtime_template=unit.runtime_template if unit else None,
                    branch=deployment.branch if deployment else None,
                    commit_sha=deployment.commit_sha if deployment else None,
                    updated_at=self._iso(unit.updated_at) if unit else None,
                    last_checked_at=self._iso(deployment.health_checked_at) if deployment else None,
                    failure_reason=failure_reason,
                    latest_error=latest_error,
                    logs_url=f"/deployments/{deployment.deployment_id}/logs" if deployment else None,
                    details={"container_name": container.name, "docker_available": docker_available} if container else {"docker_available": docker_available},
                )
            )
        return self._summarize(services)

    def _normalize_core_state(self, container: ContainerStatus | None) -> DeploymentUiState:
        if container is None:
            return "missing"
        state = (container.state or "").lower()
        health = (container.health or "").lower()
        status = (container.status or "").lower()
        if state in {"exited", "dead"} or "restarting" in status:
            return "crashed"
        if health == "unhealthy":
            return "failed"
        if state == "running" and health == "healthy":
            return "healthy"
        if state == "running" and health in {"starting"}:
            return "deploying"
        if state == "running" and not health:
            return "healthy"
        return "unknown"

    def _normalize_runtime_state(
        self,
        unit: ManagedUnit | None,
        deployment: Deployment | None,
        container: ContainerStatus | None,
        docker_available: bool,
        bootstrap_unit: dict[str, Any],
    ) -> DeploymentUiState:
        bootstrap_status = bootstrap_unit.get("status")
        if bootstrap_status in {"deploying", "pending"}:
            return "deploying"
        if bootstrap_status == "failed":
            return "failed"
        if bootstrap_status == "blocked":
            return "blocked"
        if bootstrap_status == "skipped":
            return "skipped"
        if container is not None:
            state = (container.state or "").lower()
            health = (container.health or "").lower()
            status = (container.status or "").lower()
            if state in {"exited", "dead"} or "restarting" in status:
                return "crashed"
            if health == "unhealthy":
                return "failed"
        if deployment is not None and deployment.status == "failed":
            return "failed"
        if deployment is not None and deployment.status == "healthy" and deployment.health_status == "passing":
            if docker_available and container is None:
                return "stale"
            return "healthy"
        if unit is None and deployment is None and container is None:
            return "missing"
        if deployment is not None and deployment.status in {"queued", "materializing", "building", "health_checking"}:
            return "deploying"
        return "unknown"

    def _runtime_exists(
        self,
        unit: ManagedUnit | None,
        deployment: Deployment | None,
        container: ContainerStatus | None,
        docker_available: bool,
    ) -> bool:
        if container is not None:
            return True
        if self.settings.runtime_strategy == "docker" and docker_available:
            return False
        return unit is not None or deployment is not None

    def _runtime_failure_reason(self, deployment: Deployment | None, bootstrap_unit: dict[str, Any]) -> str | None:
        return (deployment.failure_reason if deployment else None) or bootstrap_unit.get("failure_reason")

    def _latest_runtime_error(
        self,
        deployment: Deployment | None,
        failure_reason: str | None,
        container: ContainerStatus | None,
    ) -> str | None:
        if failure_reason:
            return self._trim(failure_reason)
        if deployment is not None:
            for event in reversed(self.registry.get_events(deployment.deployment_id)):
                if event.level == "error":
                    return self._trim(event.message)
            logs = self.registry.read_logs(self.settings.deployment_logs_root, deployment.deployment_id)
            if logs:
                return self._trim("\n".join(logs.splitlines()[-20:]))
        return self._container_failure(container)

    def _runtime_service_name(self, deployment: Deployment | None) -> str | None:
        runtime_ref = deployment.runtime_ref if deployment else None
        if isinstance(runtime_ref, dict):
            service_name = runtime_ref.get("service_name")
            if isinstance(service_name, str) and service_name:
                return service_name
        return None

    def _summarize(self, services: list[ServiceStatusItem]) -> ServiceGroupSummary:
        return ServiceGroupSummary(
            expected_count=len([service for service in services if service.expected]),
            existing_count=len([service for service in services if service.exists]),
            healthy_count=len([service for service in services if service.ui_state in HEALTHY_STATES]),
            warning_count=len([service for service in services if service.ui_state in WARNING_STATES]),
            broken_count=len([service for service in services if service.ui_state in BROKEN_STATES]),
            missing_count=len([service for service in services if service.ui_state == "missing"]),
            services=services,
        )

    def _overall_state(self, core: ServiceGroupSummary, runtime: ServiceGroupSummary, bootstrap_snapshot: dict[str, Any]) -> str:
        if bootstrap_snapshot.get("status") == "failed":
            return "broken"
        if core.broken_count or runtime.broken_count:
            return "broken"
        if core.warning_count or runtime.warning_count or bootstrap_snapshot.get("status") == "running":
            return "degraded"
        if core.expected_count == 0 and runtime.expected_count == 0:
            return "unknown"
        return "healthy"

    def _bootstrap_summary(self, snapshot: dict[str, Any]) -> BootstrapSummary | None:
        if not snapshot:
            return None
        return BootstrapSummary(
            run_id=snapshot.get("run_id"),
            status=snapshot.get("status", "not_started"),
            started_at=snapshot.get("started_at"),
            completed_at=snapshot.get("completed_at"),
            failure_reason=snapshot.get("failure_reason"),
            summary=snapshot.get("summary", {}),
            dependency_issues=snapshot.get("dependency_issues", {}),
        )

    def _parse_container_output(self, output: str) -> dict[str, ContainerStatus]:
        stripped = output.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
            rows = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            rows = []
            for line in stripped.splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        containers: dict[str, ContainerStatus] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            service = self._container_service(row)
            if not service:
                continue
            containers[service] = ContainerStatus(
                service=service,
                name=row.get("Name") or row.get("Names") or row.get("Service"),
                state=row.get("State"),
                status=row.get("Status"),
                health=row.get("Health"),
                exit_code=self._int_or_none(row.get("ExitCode")),
            )
        return containers

    def _container_service(self, row: dict[str, Any]) -> str | None:
        for key in ("Service", "com.docker.compose.service"):
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
        labels = row.get("Labels")
        if isinstance(labels, str):
            for part in labels.split(","):
                key, _, value = part.partition("=")
                if key == "com.docker.compose.service" and value:
                    return value
        return None

    def _container_failure(self, container: ContainerStatus | None) -> str | None:
        if container is None:
            return None
        parts = [part for part in (container.status, f"exit_code={container.exit_code}" if container.exit_code is not None else None) if part]
        return "; ".join(parts) if parts else None

    def _display_name(self, value: str) -> str:
        return value.replace("-", " ").title()

    def _trim(self, value: str) -> str:
        return value[-2000:]

    def _iso(self, value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def _int_or_none(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
