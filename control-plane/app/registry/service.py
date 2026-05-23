"""Registry persistence service."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.runtime import (
    Application,
    ApplicationAuditEvent,
    ApplicationCapability,
    ApplicationDeployment,
    Deployment,
    DeploymentEvent,
    ManagedBranch,
    ManagedUnit,
    UnitHealthSnapshot,
)
from app.schemas import (
    ActiveFrontendPreviewRuntimeResponse,
    PlatformApplicationDefinition,
    RuntimeRef,
    SeededApplicationDefinition,
    UnitManifest,
)


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

    def record_application_audit(
        self,
        application_id: str,
        event_type: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            ApplicationAuditEvent(
                application_id=application_id,
                event_type=event_type,
                message=message,
                details_json=details,
            )
        )
        self.session.flush()

    def create_deployment(self, unit_id: str, branch: str, commit_sha: str, *, delete_eligible: bool = True) -> Deployment:
        unit = self.session.get(ManagedUnit, unit_id)
        if unit is None:
            unit = ManagedUnit(
                unit_id=unit_id,
                display_name=unit_id,
                package_owner="pending",
                runtime_kind="service",
                runtime_template="python-service",
                source_path="pending",
                deployment_status="pending",
                health_status="unknown",
                discovery_metadata_json={},
                delete_eligible=delete_eligible,
            )
            self.session.add(unit)
            self.session.flush()
        deployment = Deployment(
            unit_id=unit_id,
            branch=branch,
            commit_sha=commit_sha,
            status="pending",
            health_status="unknown",
            delete_eligible=delete_eligible,
        )
        self.session.add(deployment)
        self.session.flush()
        branch_record = self.session.query(ManagedBranch).filter(ManagedBranch.branch_name == branch).one_or_none()
        if branch_record is not None and branch_record.delete_eligible:
            branch_record.associated_unit_id = unit_id
            branch_record.associated_deployment_id = deployment.deployment_id
            branch_record.updated_at = utcnow()
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
        managed_unit = self.session.get(ManagedUnit, deployment.unit_id)
        if managed_unit is None:
            managed_unit = ManagedUnit(
                unit_id=manifest.unit_id,
                display_name=manifest.display_name,
                package_owner=manifest.package_owner,
                runtime_kind=manifest.runtime_kind,
                runtime_template=manifest.runtime_template,
                source_path=manifest.source_path,
            )
            self.session.add(managed_unit)

        previous_deployment_id = managed_unit.active_deployment_id
        deployment.status = "healthy"
        deployment.health_status = "passing"
        deployment.runtime_ref = runtime_ref
        deployment.health_checked_at = utcnow()

        managed_unit.display_name = manifest.display_name
        managed_unit.package_owner = manifest.package_owner
        managed_unit.runtime_kind = manifest.runtime_kind
        managed_unit.runtime_template = manifest.runtime_template
        managed_unit.source_path = manifest.source_path
        managed_unit.active_deployment_id = deployment.deployment_id
        managed_unit.deployment_status = "healthy"
        managed_unit.health_status = "passing"
        managed_unit.discovery_metadata_json = {**manifest.discovery}
        managed_unit.updated_at = utcnow()

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

        if manifest.runtime_kind == "frontend_application" and manifest.application is not None:
            application = self.upsert_application(
                PlatformApplicationDefinition.model_validate(
                    {
                        "applicationId": manifest.application.application_id,
                        "title": manifest.application.title,
                        "description": manifest.application.description,
                        "iconKey": manifest.application.icon_key,
                        "iconColor": manifest.application.icon_color,
                        "iconBackground": manifest.application.icon_background,
                        "applicationType": manifest.application.application_type,
                        "routePath": manifest.application.route_path,
                        "loaderKey": manifest.application.loader_key,
                        "embeddedUrl": manifest.application.embedded_url,
                        "proxyBasePath": manifest.application.proxy_base_path,
                        "version": manifest.application.version,
                        "enabled": manifest.application.enabled,
                        "iframeSandbox": manifest.application.iframe_sandbox,
                        "iframeAllow": manifest.application.iframe_allow,
                        "sortOrder": manifest.application.sort_order,
                        "owner": manifest.application.owner or manifest.package_owner,
                        "capabilities": manifest.application.capabilities,
                        "healthStatus": "passing",
                        "deploymentStatus": "healthy",
                    }
                ),
                audit_event="deployment_activation",
                audit_message="Application deployment activated",
            )
            application.delete_eligible = managed_unit.delete_eligible or deployment.delete_eligible
            self._record_application_deployment(
                application_id=manifest.application.application_id,
                deployment_id=deployment.deployment_id,
                commit_sha=deployment.commit_sha,
                artifact_ref=deployment.artifact_ref,
                runtime_ref=runtime_ref,
                status="healthy",
                health_status="passing",
                delete_eligible=managed_unit.delete_eligible or deployment.delete_eligible,
            )

        self.session.flush()
        return managed_unit

    def get_units(self, *, kind: str | None = None) -> list[ManagedUnit]:
        query = self.session.query(ManagedUnit)
        if kind:
            query = query.filter(ManagedUnit.runtime_kind == kind)
        return list(query.order_by(ManagedUnit.display_name.asc()))

    def get_unit(self, unit_id: str) -> ManagedUnit | None:
        return self.session.get(ManagedUnit, unit_id)

    def seed_manifest_unit(self, manifest: UnitManifest) -> ManagedUnit:
        unit = self.session.get(ManagedUnit, manifest.unit_id)
        if unit is None:
            unit = ManagedUnit(
                unit_id=manifest.unit_id,
                display_name=manifest.display_name,
                package_owner=manifest.package_owner,
                runtime_kind=manifest.runtime_kind,
                runtime_template=manifest.runtime_template,
                source_path=manifest.source_path,
                deployment_status="pending",
                health_status="unknown",
                discovery_metadata_json={**manifest.discovery},
                delete_eligible=False,
            )
            self.session.add(unit)
        else:
            unit.display_name = manifest.display_name
            unit.package_owner = manifest.package_owner
            unit.runtime_kind = manifest.runtime_kind
            unit.runtime_template = manifest.runtime_template
            unit.source_path = manifest.source_path
            unit.discovery_metadata_json = {**manifest.discovery}
            unit.delete_eligible = False
            if not unit.active_deployment_id:
                unit.deployment_status = "pending"
                unit.health_status = "unknown"
            unit.updated_at = utcnow()
        self.session.flush()
        return unit

    def get_deployment(self, deployment_id: str) -> Deployment | None:
        return self.session.get(Deployment, deployment_id)

    def get_latest_deployment_for_unit(self, unit_id: str) -> Deployment | None:
        return (
            self.session.query(Deployment)
            .filter(Deployment.unit_id == unit_id)
            .order_by(Deployment.requested_at.desc(), Deployment.deployment_id.desc())
            .first()
        )

    def get_active_deployment_for_unit(self, unit_id: str) -> Deployment | None:
        unit = self.get_unit(unit_id)
        if unit is None or not unit.active_deployment_id:
            return None
        deployment = self.get_deployment(unit.active_deployment_id)
        if deployment is None or deployment.status != "healthy":
            return None
        return deployment

    def get_active_frontend_shell_unit(self) -> ManagedUnit | None:
        """Return the canonical active frontend shell unit, when one exists."""

        return (
            self.session.query(ManagedUnit)
            .filter(ManagedUnit.runtime_kind == "frontend_shell")
            .order_by(ManagedUnit.display_name.asc(), ManagedUnit.unit_id.asc())
            .first()
        )

    def serialize_active_frontend_preview_runtime(
        self,
        *,
        baseline_branch: str = "main",
    ) -> ActiveFrontendPreviewRuntimeResponse:
        shell_unit = self.get_active_frontend_shell_unit()
        if shell_unit is None:
            return ActiveFrontendPreviewRuntimeResponse(is_preview=False, baseline_branch=baseline_branch)

        deployment = self.get_active_deployment_for_unit(shell_unit.unit_id)
        if deployment is None and shell_unit.active_deployment_id:
            deployment = self.get_deployment(shell_unit.active_deployment_id)

        branch = deployment.branch if deployment is not None else None
        is_preview = bool(deployment is not None and branch != baseline_branch)
        branch_record = (
            self.session.query(ManagedBranch)
            .filter(ManagedBranch.branch_name == branch)
            .one_or_none()
            if branch
            else None
        )
        discovery = shell_unit.discovery_metadata_json if isinstance(shell_unit.discovery_metadata_json, dict) else {}
        target_application_id = discovery.get("target_application_id")
        if not isinstance(target_application_id, str):
            target_application_id = None

        return ActiveFrontendPreviewRuntimeResponse(
            is_preview=is_preview,
            frontend_unit_id=shell_unit.unit_id,
            active_deployment_id=deployment.deployment_id if deployment is not None else shell_unit.active_deployment_id,
            runtime_service_name=(
                deployment.runtime_ref.get("service_name")
                if deployment is not None and isinstance(deployment.runtime_ref, dict)
                else None
            ),
            branch=branch,
            commit_sha=deployment.commit_sha if deployment is not None else None,
            deployment_status=deployment.status if deployment is not None else shell_unit.deployment_status,
            health_status=deployment.health_status if deployment is not None else shell_unit.health_status,
            baseline_branch=branch_record.base_branch if branch_record is not None else baseline_branch,
            baseline_commit_sha=branch_record.base_commit_sha if branch_record is not None else None,
            preview_deployment_id=deployment.deployment_id if is_preview and deployment is not None else None,
            target_application_id=target_application_id,
        )

    def get_runtime_ref_for_unit(self, unit_id: str) -> RuntimeRef | None:
        deployment = self.get_active_deployment_for_unit(unit_id)
        if deployment is None or not deployment.runtime_ref:
            return None
        return RuntimeRef.model_validate(deployment.runtime_ref)

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

    def get_applications(self) -> list[Application]:
        return list(self.session.query(Application).order_by(Application.sort_order.asc(), Application.title.asc()))

    def get_application(self, application_id: str) -> Application | None:
        return self.session.get(Application, application_id)

    def enable_application(self, application_id: str) -> PlatformApplicationDefinition | None:
        return self._set_application_enabled_state(application_id, enabled=True)

    def disable_application(self, application_id: str) -> PlatformApplicationDefinition | None:
        return self._set_application_enabled_state(application_id, enabled=False)

    def get_application_capabilities(self, application_id: str) -> list[str]:
        rows = (
            self.session.query(ApplicationCapability)
            .filter(ApplicationCapability.application_id == application_id)
            .order_by(ApplicationCapability.capability.asc())
            .all()
        )
        return [row.capability for row in rows]

    def serialize_application(self, application: Application) -> PlatformApplicationDefinition:
        return PlatformApplicationDefinition.model_validate(
            {
                "applicationId": application.application_id,
                "title": application.title,
                "description": application.description,
                "iconKey": application.icon_key,
                "iconColor": application.icon_color,
                "iconBackground": application.icon_background,
                "applicationType": application.application_type,
                "routePath": application.route_path,
                "loaderKey": application.loader_key,
                "embeddedUrl": application.embedded_url,
                "proxyBasePath": application.proxy_base_path,
                "version": application.version,
                "enabled": application.enabled,
                "iframeSandbox": application.iframe_sandbox,
                "iframeAllow": application.iframe_allow,
                "sortOrder": application.sort_order,
                "owner": application.owner,
                "capabilities": self.get_application_capabilities(application.application_id),
                "healthStatus": application.health_status,
                "deploymentStatus": application.deployment_status,
            }
        )

    def _set_application_enabled_state(
        self,
        application_id: str,
        *,
        enabled: bool,
    ) -> PlatformApplicationDefinition | None:
        application = self.get_application(application_id)
        if application is None:
            return None
        previous_enabled = application.enabled
        application.enabled = enabled
        application.updated_at = utcnow()
        self.session.flush()
        self.record_application_audit(
            application_id,
            "enabled" if enabled else "disabled",
            "Application enabled via registry API" if enabled else "Application disabled via registry API",
            details={
                "previous_enabled": previous_enabled,
                "current_enabled": application.enabled,
            },
        )
        return self.serialize_application(application)

    def upsert_application(
        self,
        definition: PlatformApplicationDefinition,
        *,
        audit_event: str,
        audit_message: str,
    ) -> Application:
        application = self.session.get(Application, definition.application_id)
        created = application is None
        if application is None:
            application = Application(application_id=definition.application_id)
            self.session.add(application)

        application.title = definition.title
        application.description = definition.description
        application.icon_key = definition.icon_key
        application.icon_color = definition.icon_color
        application.icon_background = definition.icon_background
        application.application_type = definition.application_type
        application.route_path = definition.route_path
        application.loader_key = definition.loader_key
        application.embedded_url = definition.embedded_url
        application.proxy_base_path = definition.proxy_base_path
        application.version = definition.version
        application.enabled = definition.enabled
        application.iframe_sandbox = definition.iframe_sandbox
        application.iframe_allow = definition.iframe_allow
        application.sort_order = definition.sort_order
        application.owner = definition.owner
        application.health_status = definition.health_status
        application.deployment_status = definition.deployment_status
        application.updated_at = utcnow()
        if created:
            application.created_at = utcnow()

        self.session.execute(
            delete(ApplicationCapability).where(ApplicationCapability.application_id == definition.application_id)
        )
        for capability in definition.capabilities:
            self.session.add(
                ApplicationCapability(application_id=definition.application_id, capability=capability)
            )

        self.session.flush()
        self.record_application_audit(
            definition.application_id,
            audit_event,
            audit_message,
            details={"enabled": definition.enabled, "application_type": definition.application_type},
        )
        return application

    def get_active_application_deployment(self, application_id: str) -> ApplicationDeployment | None:
        return (
            self.session.query(ApplicationDeployment)
            .filter(
                ApplicationDeployment.application_id == application_id,
                ApplicationDeployment.status == "healthy",
            )
            .order_by(ApplicationDeployment.created_at.desc())
            .first()
        )

    def get_runtime_ref_for_application(self, application_id: str) -> RuntimeRef | None:
        deployment = self.get_active_application_deployment(application_id)
        if deployment is None or not deployment.runtime_ref:
            return None
        return RuntimeRef.model_validate(deployment.runtime_ref)

    def _record_application_deployment(
        self,
        *,
        application_id: str,
        deployment_id: str,
        commit_sha: str,
        artifact_ref: str | None,
        runtime_ref: dict[str, Any],
        status: str,
        health_status: str,
        delete_eligible: bool = True,
    ) -> None:
        previous = self.get_active_application_deployment(application_id)
        if previous is not None and previous.deployment_id != deployment_id:
            previous.status = "replaced"
            previous.updated_at = utcnow()

        row = self.session.get(ApplicationDeployment, deployment_id)
        if row is None:
            row = ApplicationDeployment(
                deployment_id=deployment_id,
                application_id=application_id,
                commit_sha=commit_sha,
                artifact_ref=artifact_ref,
                runtime_ref=runtime_ref,
                status=status,
                health_status=health_status,
                delete_eligible=delete_eligible,
            )
            self.session.add(row)
        else:
            row.application_id = application_id
            row.commit_sha = commit_sha
            row.artifact_ref = artifact_ref
            row.runtime_ref = runtime_ref
            row.status = status
            row.health_status = health_status
            row.delete_eligible = delete_eligible
            row.updated_at = utcnow()
        self.session.flush()

    def seed_builtin_applications(self, apps_source_root: Path) -> list[PlatformApplicationDefinition]:
        applications_root = apps_source_root / "mission-control-ui" / "src" / "applications"
        if not applications_root.exists():
            return []

        seed_paths = sorted(applications_root.glob("*/application.seed.json"))
        seen_ids: set[str] = set()
        seen_routes: set[str] = set()
        seen_proxy_paths: set[str] = set()
        seeded: list[PlatformApplicationDefinition] = []

        for seed_path in seed_paths:
            definition = SeededApplicationDefinition.model_validate(
                json.loads(seed_path.read_text(encoding="utf-8"))
            )
            if definition.application_id in seen_ids:
                raise ValueError(f"duplicate applicationId in seed files: {definition.application_id}")
            if definition.route_path in seen_routes:
                raise ValueError(f"duplicate routePath in seed files: {definition.route_path}")
            if definition.proxy_base_path and definition.proxy_base_path in seen_proxy_paths:
                raise ValueError(f"duplicate proxyBasePath in seed files: {definition.proxy_base_path}")

            seen_ids.add(definition.application_id)
            seen_routes.add(definition.route_path)
            if definition.proxy_base_path:
                seen_proxy_paths.add(definition.proxy_base_path)

            seeded.append(
                PlatformApplicationDefinition.model_validate(
                    {
                        **definition.model_dump(by_alias=True),
                        "healthStatus": "unknown",
                        "deploymentStatus": "seeded",
                    }
                )
            )

        for definition in seeded:
            self.upsert_application(
                definition,
                audit_event="seeded",
                audit_message="Built-in application seeded",
            )
        return seeded
