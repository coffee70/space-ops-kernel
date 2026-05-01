"""Authoritative hard-delete service for managed resources."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import Settings
from app.delete.schemas import (
    DeleteCodeRequest,
    DeleteManagedUnitRequest,
    DeleteReport,
    DeleteReportItem,
    DeleteStaleRequest,
)
from app.git.repository import ManagedGitRepository
from app.models.runtime import (
    Application,
    ApplicationAuditEvent,
    ApplicationCapability,
    ApplicationDeployment,
    Deployment,
    DeploymentEvent,
    ManagedBranch,
    ManagedUnit,
    ResourceDeleteEvent,
    UnitHealthSnapshot,
    utcnow,
)
from app.schemas import RuntimeRef
from app.services.shell import run_command


class DeleteValidationError(Exception):
    """Delete request contains unsafe or unsupported input."""


class ManagedResourceDeleteService:
    """Delete resources only after resolving persisted delete-eligible records."""

    def __init__(self, settings: Settings, repository: ManagedGitRepository, session: Session):
        self.settings = settings
        self.repository = repository
        self.session = session

    def delete_managed_unit(self, request: DeleteManagedUnitRequest) -> DeleteReport:
        report = self._new_report("managed_unit")
        self._record_event(report, "resource_delete.started", "started", resource_type="managed_unit", resource_id=request.unit_id, operation="delete", details=self._trace(request))
        unit = self.session.get(ManagedUnit, request.unit_id)
        if unit is None:
            if self._was_deleted("managed_unit", request.unit_id):
                self._add(report, "already_absent", "managed_unit", request.unit_id, "delete", "previously deleted")
                return self._complete(report, request)
            self._add(report, "refused", "managed_unit", request.unit_id, "resolve", "no authoritative managed unit record")
            self._record_event(report, "resource_delete.failed", "refused", resource_type="managed_unit", resource_id=request.unit_id, operation="resolve", message="no authoritative managed unit record", details=self._trace(request))
            return self._complete(report, request)
        if not unit.delete_eligible:
            self._add(report, "refused", "managed_unit", unit.unit_id, "authorize", "managed unit delete_eligible is false")
            self._record_event(report, "resource_delete.failed", "refused", resource_type="managed_unit", resource_id=unit.unit_id, operation="authorize", message="managed unit delete_eligible is false", details=self._trace(request))
            return self._complete(report, request)

        deployments = self._deployments_for_unit(unit.unit_id, request.deployment_id)
        application_ids = {
            row.application_id
            for deployment in deployments
            for row in self.session.query(ApplicationDeployment).filter(
                ApplicationDeployment.deployment_id == deployment.deployment_id
            )
        }
        if request.deployment_id and not deployments:
            self._add(report, "already_absent", "deployment", request.deployment_id, "resolve", "deployment row already absent")
        for deployment in deployments:
            if not deployment.delete_eligible:
                self._add(report, "refused", "deployment", deployment.deployment_id, "authorize", "deployment delete_eligible is false")
                continue
            if request.include_runtime:
                self._remove_runtime(report, deployment)
                self._remove_deployment_files(report, deployment)
            if request.include_code:
                self._remove_branch_for_deployment(report, deployment)
            if request.include_registry:
                self._delete_deployment_rows(report, deployment)

        if request.include_code:
            self._remove_source_and_manifest_paths(report, unit)
        if request.include_registry:
            self._delete_application_rows(report, application_ids)
            self.session.execute(delete(UnitHealthSnapshot).where(UnitHealthSnapshot.unit_id == unit.unit_id))
            self._add(report, "deleted", "unit_health_snapshot", unit.unit_id, "delete_rows")
            self.session.delete(unit)
            self._add(report, "deleted", "managed_unit", unit.unit_id, "delete_row")
        self.session.flush()
        return self._complete(report, request)

    def delete_code(self, request: DeleteCodeRequest) -> DeleteReport:
        report = self._new_report("code")
        branch_name = request.branch or ""
        self._record_event(report, "resource_delete.started", "started", resource_type="branch_ref", resource_id=branch_name, operation="delete", details=self._trace(request))
        for path in request.paths:
            try:
                self._validate_user_path(path)
            except DeleteValidationError as exc:
                self._add(report, "refused", "managed_source_path", path, "validate_path", str(exc))
                continue
            self._add(report, "refused", "managed_source_path", path, "authorize", "path delete requires a persisted delete-eligible branch/unit/deployment association")
        if not branch_name:
            self._add(report, "refused", "branch_ref", "", "resolve", "branch is required")
            raise DeleteValidationError("branch is required")

        branch = self.session.query(ManagedBranch).filter(ManagedBranch.branch_name == branch_name).one_or_none()
        if branch is None:
            if self._was_deleted("branch_ref", branch_name) or self._was_deleted("branch_worktree", branch_name):
                self._add(report, "already_absent", "branch_ref", branch_name, "delete_ref", "previously deleted")
                self._add(report, "already_absent", "branch_worktree", branch_name, "delete_worktree", "previously deleted")
                return self._complete(report, request)
            self._add(report, "refused", "branch_ref", branch_name, "resolve", "no authoritative managed branch record")
            return self._complete(report, request)
        if not branch.delete_eligible:
            self._add(report, "refused", "branch_ref", branch.branch_name, "authorize", "managed branch delete_eligible is false")
            return self._complete(report, request)

        self._remove_branch(report, branch)
        self.session.delete(branch)
        self._add(report, "deleted", "managed_branch", branch_name, "delete_row")
        self.session.flush()
        return self._complete(report, request)

    def delete_stale(self, request: DeleteStaleRequest, *, delete_scope_id: str | None = None) -> DeleteReport:
        report = self._new_report("stale" if delete_scope_id is None else "scope", delete_scope_id=delete_scope_id)
        cutoff = utcnow() - timedelta(minutes=request.older_than_minutes)
        self._record_event(report, "resource_delete.started", "started", operation="delete_stale", details={**self._trace(request), "cutoff": cutoff.isoformat()})
        units = (
            self.session.query(ManagedUnit)
            .filter(ManagedUnit.delete_eligible.is_(True), ManagedUnit.created_at < cutoff)
            .order_by(ManagedUnit.created_at.asc())
            .all()
        )
        branches = (
            self.session.query(ManagedBranch)
            .filter(ManagedBranch.delete_eligible.is_(True), ManagedBranch.created_at < cutoff)
            .order_by(ManagedBranch.created_at.asc())
            .all()
        )
        for unit in units:
            child_request = DeleteManagedUnitRequest(
                unit_id=unit.unit_id,
                include_code=request.include_code,
                include_runtime=request.include_runtime,
                include_registry=request.include_registry,
                include_intelligence_records=request.include_intelligence_records,
                request_id=request.request_id,
                agent_run_id=request.agent_run_id,
                tool_call_id=request.tool_call_id,
                conversation_id=request.conversation_id,
            )
            child = self.delete_managed_unit(child_request)
            self._merge(report, child)
        for branch in branches:
            if self.session.get(ManagedBranch, branch.id) is None:
                continue
            child_request = DeleteCodeRequest(
                branch=branch.branch_name,
                include_code=request.include_code,
                include_runtime=request.include_runtime,
                include_registry=request.include_registry,
                include_intelligence_records=request.include_intelligence_records,
                request_id=request.request_id,
                agent_run_id=request.agent_run_id,
                tool_call_id=request.tool_call_id,
                conversation_id=request.conversation_id,
            )
            child = self.delete_code(child_request)
            self._merge(report, child)
        return self._complete(report, request)

    def _remove_runtime(self, report: DeleteReport, deployment: Deployment) -> None:
        if not deployment.runtime_ref:
            self._add(report, "already_absent", "deployment_runtime", deployment.deployment_id, "docker_compose_rm", "runtime_ref already absent")
            return
        runtime_ref = RuntimeRef.model_validate(deployment.runtime_ref)
        if self.settings.runtime_strategy == "stub":
            self._add(report, "skipped", "deployment_runtime", deployment.deployment_id, "docker_compose_rm", "stub runtime strategy")
            return
        compose_file = self._validated_path(runtime_ref.compose_file or "", self.settings.generated_compose_root)
        command = [
            *self._compose_command(),
            "-f",
            str(self.settings.resolved_compose_file),
            "-f",
            str(compose_file),
            "rm",
            "-sf",
            runtime_ref.service_name,
        ]
        try:
            run_command(command, timeout=self.settings.deployment_command_timeout_seconds)
            self._add(report, "removed", "deployment_runtime", deployment.deployment_id, "docker_compose_rm", details={"service_name": runtime_ref.service_name})
        except Exception as exc:
            self._add(report, "errors", "deployment_runtime", deployment.deployment_id, "docker_compose_rm", str(exc))

    def _remove_deployment_files(self, report: DeleteReport, deployment: Deployment) -> None:
        paths = [
            ("generated_compose", deployment.deployment_id, self.settings.generated_compose_root / f"{deployment.deployment_id}.yaml", self.settings.generated_compose_root),
            ("generated_env", deployment.deployment_id, self.settings.generated_env_root / f"{deployment.deployment_id}.env", self.settings.generated_env_root),
            ("deployment_log", deployment.deployment_id, self.settings.deployment_logs_root / f"{deployment.deployment_id}.log", self.settings.deployment_logs_root),
            ("deployment_workspace", deployment.deployment_id, self.settings.deployment_workspaces_root / deployment.deployment_id, self.settings.deployment_workspaces_root),
            ("deployment_artifact", deployment.deployment_id, self.settings.artifacts_root / deployment.deployment_id, self.settings.artifacts_root),
        ]
        for resource_type, resource_id, path, root in paths:
            self._remove_path(report, resource_type, resource_id, path, root)

    def _remove_source_and_manifest_paths(self, report: DeleteReport, unit: ManagedUnit) -> None:
        worktree = self.settings.main_worktree_dir
        branches = self.session.query(ManagedBranch).filter(ManagedBranch.associated_unit_id == unit.unit_id).all()
        if branches:
            worktree = Path(branches[0].worktree_path)
        self._remove_path(report, "managed_source_path", unit.source_path, worktree / unit.source_path, worktree)
        self._remove_path(report, "managed_manifest", unit.unit_id, worktree / "manifests" / "units" / f"{unit.unit_id}.yaml", worktree / "manifests" / "units")

    def _remove_branch_for_deployment(self, report: DeleteReport, deployment: Deployment) -> None:
        branch = self.session.query(ManagedBranch).filter(ManagedBranch.associated_deployment_id == deployment.deployment_id).one_or_none()
        if branch is None or not branch.delete_eligible:
            return
        self._remove_branch(report, branch)
        self.session.delete(branch)
        self._add(report, "deleted", "managed_branch", branch.branch_name, "delete_row")

    def _remove_branch(self, report: DeleteReport, branch: ManagedBranch) -> None:
        worktree_path = self._validated_path(branch.worktree_path, self.settings.branch_worktrees_dir)
        try:
            result = self.repository.delete_branch_and_worktree(branch.branch_name, worktree_path)
        except Exception as exc:
            self._add(report, "errors", "branch_ref", branch.branch_name, "delete_branch_and_worktree", str(exc))
            return
        self._add(report, "removed" if result["worktree_removed"] else "already_absent", "branch_worktree", branch.branch_name, "delete_worktree", details={"path": str(worktree_path)})
        self._add(report, "removed" if result["branch_removed"] else "already_absent", "branch_ref", branch.branch_name, "delete_ref")

    def _delete_deployment_rows(self, report: DeleteReport, deployment: Deployment) -> None:
        deployment_id = deployment.deployment_id
        self.session.execute(delete(UnitHealthSnapshot).where(UnitHealthSnapshot.deployment_id == deployment_id))
        self._add(report, "deleted", "unit_health_snapshot", deployment_id, "delete_rows")
        self.session.execute(delete(DeploymentEvent).where(DeploymentEvent.deployment_id == deployment_id))
        self._add(report, "deleted", "deployment_event", deployment_id, "delete_rows")
        self.session.execute(delete(ApplicationDeployment).where(ApplicationDeployment.deployment_id == deployment_id))
        self._add(report, "deleted", "application_deployment", deployment_id, "delete_rows")
        self.session.delete(deployment)
        self._add(report, "deleted", "deployment", deployment_id, "delete_row")

    def _delete_application_rows(self, report: DeleteReport, application_ids: set[str]) -> None:
        for application_id in sorted(application_ids):
            app = self.session.get(Application, application_id)
            if app is None:
                self._add(report, "already_absent", "application", application_id, "delete_row")
                continue
            if not app.delete_eligible:
                self._add(report, "refused", "application", application_id, "authorize", "application delete_eligible is false")
                continue
            active_deployment = self.session.query(ApplicationDeployment).filter(ApplicationDeployment.application_id == app.application_id).first()
            if active_deployment is not None:
                self._add(report, "skipped", "application", app.application_id, "delete_row", "application still has deployment rows")
                continue
            app_id = app.application_id
            self.session.execute(delete(ApplicationCapability).where(ApplicationCapability.application_id == app_id))
            self._add(report, "deleted", "application_capability", app_id, "delete_rows")
            self.session.execute(delete(ApplicationAuditEvent).where(ApplicationAuditEvent.application_id == app_id))
            self._add(report, "deleted", "application_audit_event", app_id, "delete_rows")
            self.session.delete(app)
            self._add(report, "deleted", "application", app_id, "delete_row")

    def _deployments_for_unit(self, unit_id: str, deployment_id: str | None) -> list[Deployment]:
        query = self.session.query(Deployment).filter(Deployment.unit_id == unit_id)
        if deployment_id:
            query = query.filter(Deployment.deployment_id == deployment_id)
        return list(query.order_by(Deployment.requested_at.desc()))

    def _remove_path(self, report: DeleteReport, resource_type: str, resource_id: str, path: Path, root: Path) -> None:
        try:
            target = self._validated_path(str(path), root)
        except ValueError as exc:
            self._add(report, "refused", resource_type, resource_id, "remove_path", str(exc))
            return
        if not target.exists():
            self._add(report, "already_absent", resource_type, resource_id, "remove_path", details={"path": str(target)})
            return
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            self._add(report, "removed", resource_type, resource_id, "remove_path", details={"path": str(target)})
        except Exception as exc:
            self._add(report, "errors", resource_type, resource_id, "remove_path", str(exc), details={"path": str(target)})

    def _validated_path(self, value: str, root: Path) -> Path:
        if not value:
            raise ValueError("path is required")
        raw = value.strip()
        lowered = raw.lower()
        if any(token in lowered for token in ("..", "://", "*", "?", "[", "]", "{", "}", "|", ";", "$", "`")):
            raise ValueError("unsafe path input")
        if "\\" in raw or "//" in raw:
            raise ValueError("unsafe path input")
        path = Path(raw).resolve()
        approved_root = root.resolve()
        try:
            path.relative_to(approved_root)
        except ValueError as exc:
            raise ValueError("path is outside approved managed root") from exc
        if path == approved_root:
            raise ValueError("refusing to delete approved root")
        return path

    def _validate_user_path(self, value: str) -> None:
        if not value:
            raise DeleteValidationError("path is required")
        lowered = value.lower()
        if value.startswith("/") or "\\" in value or "//" in value or any(token in lowered for token in ("..", "://", "*", "?", "[", "]", "{", "}", "|", ";", "$", "`")):
            raise DeleteValidationError("unsafe path input")

    def _compose_command(self) -> list[str]:
        docker_compose = shutil.which("docker-compose")
        if docker_compose:
            return [docker_compose, "-p", self.settings.compose_project_name]
        docker_cli = shutil.which("docker")
        if docker_cli:
            return [docker_cli, "compose", "-p", self.settings.compose_project_name]
        raise FileNotFoundError("docker compose client not found")

    def _new_report(self, mode: str, delete_scope_id: str | None = None) -> DeleteReport:
        return DeleteReport(mode=mode, delete_id=f"delete_{uuid4().hex[:12]}", delete_scope_id=delete_scope_id, started_at=utcnow())

    def _add(self, report: DeleteReport, bucket: str, resource_type: str, resource_id: str, operation: str, reason: str | None = None, details: dict[str, Any] | None = None) -> None:
        status = "error" if bucket == "errors" else bucket[:-1] if bucket.endswith("s") else bucket
        if bucket == "already_absent":
            status = "already_absent"
        item = DeleteReportItem(resource_type=resource_type, resource_id=resource_id, operation=operation, status=status, reason=reason, details=details or {})
        getattr(report, bucket).append(item)
        event_type = "resource_delete.failed" if bucket == "errors" else "resource_delete.skipped" if bucket in {"skipped", "refused", "already_absent"} else "resource_delete.completed"
        self._record_event(report, event_type, status, resource_type=resource_type, resource_id=resource_id, operation=operation, message=reason, details=details)

    def _complete(self, report: DeleteReport, request: Any) -> DeleteReport:
        report.completed_at = utcnow()
        duration_ms = int((report.completed_at - report.started_at).total_seconds() * 1000)
        self._record_event(
            report,
            "resource_delete.completed" if not report.errors and not report.refused else "resource_delete.failed",
            "completed" if not report.errors and not report.refused else "failed",
            operation="complete",
            message=None,
            details={
                **self._trace(request),
                "deleted_count": len(report.deleted),
                "removed_count": len(report.removed),
                "already_absent_count": len(report.already_absent),
                "skipped_count": len(report.skipped),
                "refused_count": len(report.refused),
                "error_count": len(report.errors),
                "duration_ms": duration_ms,
            },
        )
        self.session.flush()
        return report

    def _merge(self, report: DeleteReport, child: DeleteReport) -> None:
        for name in ("deleted", "removed", "already_absent", "skipped", "refused", "errors", "warnings"):
            getattr(report, name).extend(getattr(child, name))

    def _record_event(
        self,
        report: DeleteReport,
        event_type: str,
        status: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        operation: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        trace = details or {}
        self.session.add(
            ResourceDeleteEvent(
                delete_id=report.delete_id,
                delete_scope_id=report.delete_scope_id,
                event_type=event_type,
                mode=report.mode,
                resource_type=resource_type,
                resource_id=resource_id,
                operation=operation,
                status=status,
                message=message,
                details_json=self._redact(trace),
                request_id=trace.get("request_id"),
                agent_run_id=trace.get("agent_run_id"),
                tool_call_id=trace.get("tool_call_id"),
                conversation_id=trace.get("conversation_id"),
                created_at=datetime.now(timezone.utc),
            )
        )

    def _was_deleted(self, resource_type: str, resource_id: str) -> bool:
        return (
            self.session.query(ResourceDeleteEvent)
            .filter(
                ResourceDeleteEvent.resource_type == resource_type,
                ResourceDeleteEvent.resource_id == resource_id,
                ResourceDeleteEvent.status.in_(("deleted", "removed")),
            )
            .first()
            is not None
        )

    @staticmethod
    def _trace(request: Any) -> dict[str, Any]:
        return {
            "request_id": getattr(request, "request_id", None),
            "agent_run_id": getattr(request, "agent_run_id", None),
            "tool_call_id": getattr(request, "tool_call_id", None),
            "conversation_id": getattr(request, "conversation_id", None),
        }

    def _redact(self, value: Any) -> Any:
        blocked = ("authorization", "api_key", "token", "cookie", "password", "secret", "database_url", "openai")
        if isinstance(value, dict):
            return {key: ("[redacted]" if any(block in key.lower() for block in blocked) else self._redact(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + "...[truncated]"
        return value
