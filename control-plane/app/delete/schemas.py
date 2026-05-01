"""Schemas for managed resource delete routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DeleteRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_code: bool = True
    include_runtime: bool = True
    include_registry: bool = True
    include_intelligence_records: bool = False
    request_id: str | None = None
    agent_run_id: str | None = None
    tool_call_id: str | None = None
    conversation_id: str | None = None


class DeleteManagedUnitRequest(DeleteRequestBase):
    unit_id: str = Field(min_length=1, max_length=255)
    deployment_id: str | None = Field(default=None, min_length=1, max_length=64)


class DeleteCodeRequest(DeleteRequestBase):
    branch: str | None = Field(default=None, min_length=1, max_length=255)
    paths: list[str] = Field(default_factory=list)


class DeleteScopeRequest(DeleteRequestBase):
    older_than_minutes: int | None = Field(default=None, ge=1)


class DeleteStaleRequest(DeleteRequestBase):
    older_than_minutes: int = Field(ge=1)


class DeleteReportItem(BaseModel):
    resource_type: str
    resource_id: str
    operation: str
    status: Literal["deleted", "removed", "already_absent", "skipped", "refused", "error"]
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class DeleteReport(BaseModel):
    mode: str = Field(exclude=True)
    delete_id: str
    delete_scope_id: str | None = None
    deleted: list[DeleteReportItem] = Field(default_factory=list)
    removed: list[DeleteReportItem] = Field(default_factory=list)
    already_absent: list[DeleteReportItem] = Field(default_factory=list)
    skipped: list[DeleteReportItem] = Field(default_factory=list)
    refused: list[DeleteReportItem] = Field(default_factory=list)
    errors: list[DeleteReportItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime | None = None
