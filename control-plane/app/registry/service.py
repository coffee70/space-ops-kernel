"""Registry persistence service."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.runtime import Deployment, DeploymentEvent, ManagedUnit, UnitHealthSnapshot
from app.schemas import UnitManifest


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RegistryService:
    """Canonical registry read/write model."""

    def __init__(self, session: Session):
        self.session = session

    def record_event(
        self,
        deployment_id: str,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            DeploymentEvent(
                deployment_id=deployment_id,
                event_type=event_type,
                level=level,
                message=message,
                details_json=details,
            )
        )
        self.session.flush()

    def create_deployment(self, unit_id: str, branch: str, commit_sha: str) -> Deployment:
        unit = self.session.get(ManagedUnit, unit_id)
        if unit is None:
            unit = ManagedUnit(
                unit_id=unit_id,
                display_name=unit_id,
                package_owner="pending",
                unit_kind="pending",
                runtime_template="pending",
                source_path="pending",
                deployment_status="pending",
                health_status="unknown",
                discovery_metadata_json={},
            )
            self.session.add(unit)
            self.session.flush()
        deployment = Deployment(unit_id=unit_id, branch=branch, commit_sha=commit_sha, status="pending", health_status="unknown")
        self.session.add(deployment)
        self.session.flush()
        self.record_event(deployment.deployment_id, "requested", "Deployment requested")
        return deployment

    def mark_build_started(self, deployment: Deployment) -> None:
        deployment.status = "building"
        deployment.build_started_at = utcnow()
        self.record_event(deployment.deployment_id, "build_started", "Build started")
        self.session.flush()

    def mark_build_finished(self, deployment: Deployment, *, artifact_ref: str) -> None:
        deployment.build_finished_at = utcnow()
        deployment.artifact_ref = artifact_ref
        self.record_event(deployment.deployment_id, "build_finished", "Build finished", details={"artifact_ref": artifact_ref})
        self.session.flush()

    def mark_failed(self, deployment: Deployment, reason: str) -> None:
        deployment.status = "failed"
        deployment.health_status = "failing"
        deployment.failure_reason = reason
        deployment.health_checked_at = utcnow()
        self.record_event(deployment.deployment_id, "failed", reason, level="error")
        unit = self.session.get(ManagedUnit, deployment.unit_id)
        if unit and unit.active_deployment_id != deployment.deployment_id:
            if unit.active_deployment_id:
                unit.deployment_status = "healthy"
                unit.health_status = "passing"
            else:
                unit.deployment_status = "failed"
                unit.health_status = "failing"
            unit.updated_at = utcnow()
        self.session.flush()

    def register_healthy_deployment(
        self,
        deployment: Deployment,
        manifest: UnitManifest,
        runtime_ref: dict[str, Any],
    ) -> ManagedUnit:
        previous_active = self.session.get(ManagedUnit, deployment.unit_id)
        if previous_active is None:
            previous_active = ManagedUnit(
                unit_id=manifest.unit_id,
                display_name=manifest.display_name,
                package_owner=manifest.package_owner,
                unit_kind=manifest.unit_kind,
                runtime_template=manifest.runtime_template,
                source_path=manifest.source_path,
            )
            self.session.add(previous_active)

        previous_deployment_id = previous_active.active_deployment_id
        deployment.status = "healthy"
        deployment.health_status = "passing"
        deployment.runtime_ref = runtime_ref
        deployment.health_checked_at = utcnow()

        previous_active.display_name = manifest.display_name
        previous_active.package_owner = manifest.package_owner
        previous_active.unit_kind = manifest.unit_kind
        previous_active.runtime_template = manifest.runtime_template
        previous_active.source_path = manifest.source_path
        previous_active.active_deployment_id = deployment.deployment_id
        previous_active.deployment_status = "healthy"
        previous_active.health_status = "passing"
        previous_active.discovery_metadata_json = {
            **manifest.discovery,
            "runtime_ref": runtime_ref,
        }
        previous_active.updated_at = utcnow()

        self.record_event(
            deployment.deployment_id,
            "healthy",
            "Deployment passed health checks and was registered",
            details={"runtime_ref": runtime_ref},
        )
        self.session.add(
            UnitHealthSnapshot(
                unit_id=manifest.unit_id,
                deployment_id=deployment.deployment_id,
                status="passing",
                details_json=runtime_ref,
            )
        )

        if previous_deployment_id and previous_deployment_id != deployment.deployment_id:
            previous_deployment = self.session.get(Deployment, previous_deployment_id)
            if previous_deployment and previous_deployment.status == "healthy":
                previous_deployment.status = "replaced"
        self.session.flush()
        return previous_active

    def get_units(self, *, kind: str | None = None) -> list[ManagedUnit]:
        query = self.session.query(ManagedUnit)
        if kind:
            query = query.filter(ManagedUnit.unit_kind == kind)
        return list(query.order_by(ManagedUnit.display_name.asc()))

    def get_unit(self, unit_id: str) -> ManagedUnit | None:
        return self.session.get(ManagedUnit, unit_id)

    def get_deployment(self, deployment_id: str) -> Deployment | None:
        return self.session.get(Deployment, deployment_id)

    def get_events(self, deployment_id: str) -> list[DeploymentEvent]:
        return list(
            self.session.query(DeploymentEvent)
            .filter(DeploymentEvent.deployment_id == deployment_id)
            .order_by(DeploymentEvent.id.asc())
        )

    def read_logs(self, logs_root: Path, deployment_id: str) -> str:
        log_path = logs_root / f"{deployment_id}.log"
        if not log_path.exists():
            return ""
        return log_path.read_text(encoding="utf-8")
