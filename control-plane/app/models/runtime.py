"""Runtime registry ORM models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    """Return timezone-aware utc timestamp."""

    return datetime.now(timezone.utc)


class ManagedUnit(Base):
    """Canonical runtime registry entry."""

    __tablename__ = "managed_units"

    unit_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    package_owner: Mapped[str] = mapped_column(String(64), nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_template: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    active_deployment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_status: Mapped[str] = mapped_column(String(64), nullable=False, default="inactive")
    health_status: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    discovery_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)


class Deployment(Base):
    """Deployment record."""

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


class DeploymentEvent(Base):
    """Deployment event log."""

    __tablename__ = "deployment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.deployment_id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class UnitHealthSnapshot(Base):
    """Point-in-time unit health."""

    __tablename__ = "unit_health_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("managed_units.unit_id"), nullable=False, index=True)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.deployment_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

