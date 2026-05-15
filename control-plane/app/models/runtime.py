"""Runtime registry ORM models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    """Return timezone-aware utc timestamp."""

    return datetime.now(timezone.utc)


class ManagedUnit(Base):
    """Canonical managed runtime entry."""

    __tablename__ = "managed_units"

    unit_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    package_owner: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_template: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    active_deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_status: Mapped[str] = mapped_column(String(64), nullable=False, default="inactive")
    health_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    discovery_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    delete_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Deployment(Base):
    """Managed runtime deployment record."""

    __tablename__ = "deployments"

    deployment_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: f"dep_{uuid4().hex[:12]}")
    unit_id: Mapped[str] = mapped_column(ForeignKey("managed_units.unit_id"), nullable=False, index=True)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="pending")
    health_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    build_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    build_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    health_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    runtime_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    delete_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class DeploymentEvent(Base):
    """Managed runtime deployment event log."""

    __tablename__ = "deployment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.deployment_id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class UnitHealthSnapshot(Base):
    """Point-in-time managed runtime health."""

    __tablename__ = "unit_health_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("managed_units.unit_id"), nullable=False, index=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.deployment_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class RuntimeBootstrapRun(Base):
    """Runtime bootstrap run state."""

    __tablename__ = "runtime_bootstrap_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependency_issues_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RuntimeBootstrapUnit(Base):
    """Per-unit runtime bootstrap status."""

    __tablename__ = "runtime_bootstrap_units"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runtime_bootstrap_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    unit_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Application(Base):
    """Database-backed platform application registry entry."""

    __tablename__ = "applications"
    __table_args__ = (
        CheckConstraint(
            "application_type IN ('native', 'embedded')",
            name="ck_applications_application_type",
        ),
        CheckConstraint(
            "char_length(application_id) > 0",
            name="ck_applications_application_id_nonempty",
        ),
        CheckConstraint(
            "route_path LIKE '/apps/%'",
            name="ck_applications_route_path_prefix",
        ),
        CheckConstraint(
            "((application_type = 'native' AND loader_key IS NOT NULL AND embedded_url IS NULL AND proxy_base_path IS NULL) "
            "OR (application_type = 'embedded' AND loader_key IS NULL AND (embedded_url IS NOT NULL OR proxy_base_path IS NOT NULL)))",
            name="ck_applications_transport_contract",
        ),
        CheckConstraint(
            "route_path = '/apps/' || application_id",
            name="ck_applications_route_path_matches_application_id",
        ),
        CheckConstraint(
            "proxy_base_path IS NULL OR proxy_base_path LIKE '/runtime-applications/%'",
            name="ck_applications_proxy_base_path_prefix",
        ),
    )

    application_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(320), nullable=False)
    icon_key: Mapped[str] = mapped_column(String(64), nullable=False)
    icon_color: Mapped[str] = mapped_column(String(64), nullable=False)
    icon_background: Mapped[str] = mapped_column(String(64), nullable=False)
    application_type: Mapped[str] = mapped_column(String(32), nullable=False)
    route_path: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    loader_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedded_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_base_path: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    iframe_sandbox: Mapped[str | None] = mapped_column(String(255), nullable=True)
    iframe_allow: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    health_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    deployment_status: Mapped[str] = mapped_column(String(64), nullable=False, default="seeded")
    delete_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ApplicationCapability(Base):
    """Capabilities exposed by an application registry entry."""

    __tablename__ = "application_capabilities"

    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id", ondelete="CASCADE"),
        primary_key=True,
    )
    capability: Mapped[str] = mapped_column(String(64), primary_key=True)


class ApplicationDeployment(Base):
    """Frontend application deployment runtime metadata."""

    __tablename__ = "application_deployments"

    deployment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    runtime_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    health_status: Mapped[str] = mapped_column(String(64), nullable=False)
    delete_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ApplicationAuditEvent(Base):
    """Application registry audit trail."""

    __tablename__ = "application_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.application_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ManagedBranch(Base):
    """Authoritative record for branches created by managed fork APIs."""

    __tablename__ = "managed_branches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    branch_name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    repository_root: Mapped[str] = mapped_column(String(1024), nullable=False)
    worktree_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    base_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    associated_unit_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    associated_deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    delete_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class ResourceDeleteEvent(Base):
    """Durable audit stream for managed resource hard-delete operations."""

    __tablename__ = "resource_delete_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    delete_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    operation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
