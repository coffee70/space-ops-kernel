"""Registry persistence service."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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
    ValidationCheck,
)
from app.schemas import (
    ActiveFrontendPreviewRuntimeResponse,
    DeploymentIntent,
    FrontendRuntimeDeployment,
    FrontendRuntimeEffectiveState,
    FrontendRuntimeStatusResponse,
    PlatformApplicationDefinition,
    RuntimeRef,
    SeededApplicationDefinition,
    UnitManifest,
    ValidationCheckResponse,
    ValidationStep,
)

NON_TERMINAL_DEPLOYMENT_STATUSES = frozenset({"queued", "materializing", "building", "health_checking"})
TERMINAL_DEPLOYMENT_STATUSES = frozenset({"healthy", "failed", "replaced"})


def compute_effective_frontend_runtime_state(
    *,
    active: FrontendRuntimeDeployment | None,
    pending: FrontendRuntimeDeployment | None,
    last_terminal: FrontendRuntimeDeployment | None,
    baseline_branch: str = "main",
) -> FrontendRuntimeEffectiveState:
    """Compute frontend runtime state from control-plane deployment truth."""

    if pending is not None:
        if pending.deployment_intent == DeploymentIntent.REVERT_TO_BASELINE.value:
            return "baseline_reverting"
        if pending.deployment_intent == DeploymentIntent.DEPLOY_PREVIEW.value:
            return "preview_deploying"
        if pending.branch == baseline_branch:
            return "baseline_reverting"
        return "preview_deploying"

    if active is not None and active.deployment_status == "healthy" and active.health_status == "passing":
        if active.mode == "baseline":
            return "baseline_active"
        if (
            active.mode == "preview"
            and last_terminal is not None
            and last_terminal.deployment_status == "failed"
            and last_terminal.deployment_intent == DeploymentIntent.REVERT_TO_BASELINE.value
        ):
            return "baseline_revert_failed"
        if active.mode == "preview":
            return "preview_active"

    if last_terminal is not None and last_terminal.deployment_status == "failed":
        if last_terminal.deployment_intent == DeploymentIntent.REVERT_TO_BASELINE.value:
            return "baseline_revert_failed"
        if last_terminal.deployment_intent == DeploymentIntent.DEPLOY_PREVIEW.value:
            return "preview_deploy_failed"

    return "unknown"


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

    def get_in_progress_deployment_for_unit(self, unit_id: str) -> Deployment | None:
        return (
            self.session.query(Deployment)
            .filter(Deployment.unit_id == unit_id, Deployment.status.in_(NON_TERMINAL_DEPLOYMENT_STATUSES))
            .order_by(Deployment.requested_at.asc(), Deployment.deployment_id.asc())
            .first()
        )

    def create_deployment(
        self,
        unit_id: str,
        branch: str,
        commit_sha: str,
        *,
        deployment_intent: DeploymentIntent | str = DeploymentIntent.NORMAL_DEPLOY,
        delete_eligible: bool = True,
    ) -> Deployment:
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
            deployment_intent=str(deployment_intent.value if isinstance(deployment_intent, DeploymentIntent) else deployment_intent),
            status="queued",
            health_status="pending",
            delete_eligible=delete_eligible,
        )
        self.session.add(deployment)
        self.session.flush()
        branch_record = self.session.query(ManagedBranch).filter(ManagedBranch.branch_name == branch).one_or_none()
        if branch_record is not None and branch_record.delete_eligible:
            branch_record.associated_unit_id = unit_id
            branch_record.associated_deployment_id = deployment.deployment_id
            branch_record.updated_at = utcnow()
        self.record_event(deployment.deployment_id, "queued", "Deployment queued")
        return deployment

    def claim_next_queued_deployment(self) -> Deployment | None:
        deployment = (
            self.session.query(Deployment)
            .filter(Deployment.status == "queued")
            .order_by(Deployment.requested_at.asc(), Deployment.deployment_id.asc())
            .with_for_update(skip_locked=True)
            .first()
        )
        if deployment is None:
            return None
        deployment.status = "materializing"
        deployment.build_started_at = utcnow()
        self.record_event(deployment.deployment_id, "claimed", "Deployment claimed by worker")
        self.session.flush()
        return deployment

    def mark_materializing(self, deployment: Deployment) -> None:
        deployment.status = "materializing"
        deployment.build_started_at = deployment.build_started_at or utcnow()
        self.record_event(deployment.deployment_id, "materializing", "Materializing source")
        self.session.flush()

    def mark_build_started(self, deployment: Deployment) -> None:
        deployment.status = "building"
        deployment.build_started_at = deployment.build_started_at or utcnow()
        self.record_event(deployment.deployment_id, "build_started", "Build started")
        self.session.flush()

    def mark_build_finished(self, deployment: Deployment, *, artifact_ref: str) -> None:
        deployment.build_finished_at = utcnow()
        deployment.artifact_ref = artifact_ref
        self.record_event(deployment.deployment_id, "build_finished", "Build finished", details={"artifact_ref": artifact_ref})
        self.session.flush()

    def mark_health_checking(self, deployment: Deployment) -> None:
        deployment.status = "health_checking"
        deployment.health_status = "pending"
        self.record_event(deployment.deployment_id, "health_checking", "Health checks started")
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

    def fail_stale_deployments(self, *, older_than_minutes: int, logs_root: Path) -> int:
        cutoff = utcnow() - timedelta(minutes=older_than_minutes)
        stale_deployments = (
            self.session.query(Deployment)
            .filter(Deployment.status.in_(NON_TERMINAL_DEPLOYMENT_STATUSES), Deployment.requested_at < cutoff)
            .order_by(Deployment.requested_at.asc())
            .all()
        )
        for deployment in stale_deployments:
            reason = f"Deployment marked failed by worker startup cleanup after {older_than_minutes} minutes without completion."
            self.append_log(logs_root, deployment.deployment_id, f"{reason}\n")
            self.mark_failed(deployment, reason)
        return len(stale_deployments)

    @staticmethod
    def append_log(logs_root: Path, deployment_id: str, content: str) -> None:
        if not content:
            return
        logs_root.mkdir(parents=True, exist_ok=True)
        with (logs_root / f"{deployment_id}.log").open("a", encoding="utf-8") as handle:
            handle.write(content)

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

    def get_latest_terminal_deployment_for_unit(self, unit_id: str) -> Deployment | None:
        return (
            self.session.query(Deployment)
            .filter(Deployment.unit_id == unit_id, Deployment.status.in_(TERMINAL_DEPLOYMENT_STATUSES))
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

    def create_validation_check(
        self,
        *,
        deployment_id: str | None,
        unit_id: str | None,
        check_type: str,
        target_ref: str,
        expected_json: dict[str, Any] | None = None,
        failure_layer: str | None = None,
    ) -> ValidationCheck:
        check = ValidationCheck(
            deployment_id=deployment_id,
            unit_id=unit_id,
            check_type=check_type,
            target_ref=target_ref,
            status="pending",
            expected_json=expected_json,
            failure_layer=failure_layer,
        )
        self.session.add(check)
        self.session.flush()
        return check

    def mark_validation_running(self, check: ValidationCheck) -> None:
        check.status = "running"
        check.updated_at = utcnow()
        self.session.flush()

    def mark_validation_passed(
        self,
        check: ValidationCheck,
        *,
        observed_json: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        check.status = "passed"
        check.observed_json = observed_json
        check.message = message
        check.updated_at = utcnow()
        self.session.flush()

    def mark_validation_failed(
        self,
        check: ValidationCheck,
        *,
        observed_json: dict[str, Any] | None = None,
        failure_layer: str | None = None,
        message: str | None = None,
    ) -> None:
        check.status = "failed"
        check.observed_json = observed_json
        check.failure_layer = failure_layer or check.failure_layer or "unknown"
        check.message = message
        check.updated_at = utcnow()
        self.session.flush()

    def get_validation_checks_for_deployment(self, deployment_id: str) -> list[ValidationCheck]:
        return list(
            self.session.query(ValidationCheck)
            .filter(ValidationCheck.deployment_id == deployment_id)
            .order_by(ValidationCheck.created_at.asc(), ValidationCheck.id.asc())
        )

    def clear_validation_checks_for_deployment(self, deployment_id: str) -> int:
        count = (
            self.session.query(ValidationCheck)
            .filter(ValidationCheck.deployment_id == deployment_id)
            .delete(synchronize_session=False)
        )
        self.session.flush()
        return int(count or 0)

    def serialize_validation_check(self, check: ValidationCheck) -> ValidationCheckResponse:
        return ValidationCheckResponse(
            id=check.id,
            deployment_id=check.deployment_id,
            unit_id=check.unit_id,
            check_type=check.check_type,
            target_ref=check.target_ref,
            status=check.status,
            expected_json=check.expected_json,
            observed_json=check.observed_json,
            failure_layer=check.failure_layer,
            message=check.message,
        )

    def summarize_validation_counts(self, deployment_id: str) -> dict[str, int]:
        counts = {status: 0 for status in ("passed", "failed", "running", "skipped")}
        for check in self.get_validation_checks_for_deployment(deployment_id):
            if check.status in counts:
                counts[check.status] += 1
        return counts

    def summarize_validation_status(self, deployment_id: str) -> str:
        statuses = [check.status for check in self.get_validation_checks_for_deployment(deployment_id)]
        if not statuses:
            return "not_run"
        if any(status == "running" for status in statuses):
            return "running"
        if any(status == "failed" for status in statuses):
            return "failed"
        if all(status == "passed" for status in statuses):
            return "passed"
        if any(status == "passed" for status in statuses):
            return "partially_validated"
        return "not_run"

    def latest_validation_failure_message(self, deployment_id: str) -> str | None:
        check = (
            self.session.query(ValidationCheck)
            .filter(ValidationCheck.deployment_id == deployment_id, ValidationCheck.status == "failed")
            .order_by(ValidationCheck.updated_at.desc(), ValidationCheck.id.desc())
            .first()
        )
        return check.message if check is not None else None

    def success_claim_allowed(self, deployment: Deployment | None) -> bool:
        return bool(
            deployment is not None
            and deployment.status == "healthy"
            and deployment.health_status == "passing"
            and self.summarize_validation_status(deployment.deployment_id) == "passed"
        )

    def suggested_validation_steps_for_deployment(self, deployment: Deployment) -> list[ValidationStep]:
        unit = self.session.get(ManagedUnit, deployment.unit_id)
        if unit is None:
            return []
        discovery = unit.discovery_metadata_json if isinstance(unit.discovery_metadata_json, dict) else {}
        if unit.runtime_kind == "service":
            service_slug = discovery.get("service_slug") if isinstance(discovery.get("service_slug"), str) else unit.unit_id
            health_path = discovery.get("health_endpoint") if isinstance(discovery.get("health_endpoint"), str) else None
            if not health_path and isinstance(deployment.runtime_ref, dict):
                health = deployment.runtime_ref.get("health")
                if isinstance(health, dict) and isinstance(health.get("path"), str):
                    health_path = health["path"]
            health_path = health_path or "/health"
            steps = [
                ValidationStep(
                    check_type="service_health_gateway",
                    path=f"/internal/runtime-services/{service_slug}{health_path}",
                    expected_status=200,
                    failure_layer="gateway",
                )
            ]
            primary_routes = discovery.get("primary_routes")
            if isinstance(primary_routes, list):
                for index, route in enumerate(primary_routes):
                    if not isinstance(route, dict):
                        continue
                    method = route.get("method", "GET")
                    path = route.get("path")
                    if method != "GET" or not isinstance(path, str) or not path.startswith("/"):
                        continue
                    expected_status = route.get("expected_status") or 200
                    if not isinstance(expected_status, int):
                        expected_status = 200
                    steps.append(
                        ValidationStep(
                            check_type=str(route.get("check_type") or f"service_primary_route_{index + 1}"),
                            path=f"/internal/runtime-services/{service_slug}{path}",
                            expected_status=expected_status,
                            expected_body_contains=(
                                route.get("expected_body_contains")
                                if isinstance(route.get("expected_body_contains"), dict)
                                else None
                            ),
                            failure_layer="service_route",
                        )
                    )
            return steps
        if unit.runtime_kind == "frontend_application":
            application_id = None
            for application in self.get_applications():
                app_deployment = self.get_active_application_deployment(application.application_id)
                if app_deployment is not None and app_deployment.deployment_id == deployment.deployment_id:
                    application_id = application.application_id
                    break
            if application_id:
                return [
                    ValidationStep(
                        check_type="frontend_application_route",
                        path=f"/runtime-applications/{application_id}",
                        expected_status=200,
                        failure_layer="frontend_route",
                    )
                ]
        if unit.runtime_kind == "frontend_shell":
            return [
                ValidationStep(
                    check_type="frontend_shell_route",
                    path="/frontend-shell",
                    expected_status=200,
                    failure_layer="frontend_route",
                )
            ]
        return []

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
            validation_status=self.summarize_validation_status(deployment.deployment_id) if deployment is not None else "not_run",
            validation_summary=self.summarize_validation_counts(deployment.deployment_id) if deployment is not None else {},
            success_claim_allowed=self.success_claim_allowed(deployment),
            last_validation_message=self.latest_validation_failure_message(deployment.deployment_id) if deployment is not None else None,
        )

    def serialize_frontend_runtime_status(
        self,
        *,
        baseline_branch: str = "main",
    ) -> FrontendRuntimeStatusResponse:
        shell_unit = self.get_active_frontend_shell_unit()
        if shell_unit is None:
            return FrontendRuntimeStatusResponse(
                frontend_unit_id=None,
                target_application_id=None,
                baseline_branch=baseline_branch,
                effective_state="unknown",
            )

        active_deployment = self.get_active_deployment_for_unit(shell_unit.unit_id)
        if active_deployment is None and shell_unit.active_deployment_id:
            active_deployment = self.get_deployment(shell_unit.active_deployment_id)
        pending_deployment = self.get_in_progress_deployment_for_unit(shell_unit.unit_id)
        last_terminal_deployment = self.get_latest_terminal_deployment_for_unit(shell_unit.unit_id)

        baseline_commit_sha = self._baseline_commit_sha_for_runtime(
            baseline_branch=baseline_branch,
            deployments=[pending_deployment, active_deployment, last_terminal_deployment],
        )
        discovery = shell_unit.discovery_metadata_json if isinstance(shell_unit.discovery_metadata_json, dict) else {}
        target_application_id = discovery.get("target_application_id")
        if not isinstance(target_application_id, str):
            target_application_id = None

        active = self._serialize_frontend_runtime_deployment(active_deployment, baseline_branch=baseline_branch)
        pending = self._serialize_frontend_runtime_deployment(pending_deployment, baseline_branch=baseline_branch)
        last_terminal = self._serialize_frontend_runtime_deployment(last_terminal_deployment, baseline_branch=baseline_branch)
        effective_state = compute_effective_frontend_runtime_state(
            active=active,
            pending=pending,
            last_terminal=last_terminal,
            baseline_branch=baseline_branch,
        )
        return FrontendRuntimeStatusResponse(
            frontend_unit_id=shell_unit.unit_id,
            target_application_id=target_application_id,
            baseline_branch=baseline_branch,
            baseline_commit_sha=baseline_commit_sha,
            active=active,
            pending=pending,
            last_terminal=last_terminal,
            effective_state=effective_state,
        )

    def _baseline_commit_sha_for_runtime(
        self,
        *,
        baseline_branch: str,
        deployments: list[Deployment | None],
    ) -> str | None:
        for deployment in deployments:
            if deployment is None or not deployment.branch:
                continue
            branch_record = self.session.query(ManagedBranch).filter(ManagedBranch.branch_name == deployment.branch).one_or_none()
            if branch_record is not None and branch_record.base_commit_sha:
                return branch_record.base_commit_sha
        baseline_record = self.session.query(ManagedBranch).filter(ManagedBranch.branch_name == baseline_branch).one_or_none()
        return baseline_record.base_commit_sha if baseline_record is not None else None

    @staticmethod
    def _runtime_mode(branch: str | None, *, baseline_branch: str) -> str:
        if not branch:
            return "unknown"
        return "baseline" if branch == baseline_branch else "preview"

    def _serialize_frontend_runtime_deployment(
        self,
        deployment: Deployment | None,
        *,
        baseline_branch: str,
    ) -> FrontendRuntimeDeployment | None:
        if deployment is None:
            return None
        mode = self._runtime_mode(deployment.branch, baseline_branch=baseline_branch)
        return FrontendRuntimeDeployment(
            deployment_id=deployment.deployment_id,
            runtime_service_name=(
                deployment.runtime_ref.get("service_name")
                if isinstance(deployment.runtime_ref, dict)
                else None
            ),
            branch=deployment.branch,
            commit_sha=deployment.commit_sha,
            deployment_status=deployment.status,
            health_status=deployment.health_status,
            deployment_intent=deployment.deployment_intent or DeploymentIntent.NORMAL_DEPLOY.value,
            mode=mode,
            is_preview=mode == "preview",
            failure_reason=deployment.failure_reason,
            validation_status=self.summarize_validation_status(deployment.deployment_id),
            validation_summary=self.summarize_validation_counts(deployment.deployment_id),
            success_claim_allowed=self.success_claim_allowed(deployment),
            last_validation_message=self.latest_validation_failure_message(deployment.deployment_id),
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
