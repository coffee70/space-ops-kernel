"""Pydantic schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    """Common code-operation metadata envelope."""

    branch: str
    commit_sha: str
    path: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class ActorEnvelope(BaseModel):
    """Actor metadata."""

    actor_id: str
    display_name: str
    anonymous: bool


class FileWriteRequest(BaseModel):
    """Write request."""

    branch: str
    path: str
    content: str


class BranchCreateRequest(BaseModel):
    """Branch create request."""

    branch: str
    from_branch: str = "main"


class CommitCreateRequest(BaseModel):
    """Commit request."""

    branch: str
    message: str


class DeploymentSubmissionRequest(BaseModel):
    """Deployment request."""

    unit_id: str
    branch: str = "main"
    commit_sha: str | None = None


class BuildSpec(BaseModel):
    """Build contract."""

    command: str


class RunSpec(BaseModel):
    """Run contract."""

    command: str


class HealthSpec(BaseModel):
    """Health contract."""

    type: Literal["http"]
    path: str
    port: int


class RuntimeTransport(BaseModel):
    """Transport settings for an active runtime."""

    scheme: Literal["http"] = "http"
    host: str
    port: int


class RuntimeHealth(BaseModel):
    """Health endpoint settings for an active runtime."""

    path: str


class RuntimeProxy(BaseModel):
    """Proxy path settings for an active runtime."""

    base_path: str = ""


class RuntimeRef(BaseModel):
    """Structured runtime connectivity metadata."""

    service_name: str
    compose_file: str | None = None
    env_file: str | None = None
    transport: RuntimeTransport
    health: RuntimeHealth
    proxy: RuntimeProxy = Field(default_factory=RuntimeProxy)


class UnitManifest(BaseModel):
    """Unit manifest."""

    unit_id: str
    display_name: str
    package_owner: Literal["space-ops-platform", "space-ops-apps"]
    unit_kind: Literal["service", "module"]
    runtime_template: Literal["python-service", "node-service", "frontend-module"]
    source_path: str
    build: BuildSpec
    run: RunSpec
    health: HealthSpec
    discovery: dict[str, Any] = Field(default_factory=dict)


class ScaffoldRequest(BaseModel):
    """Scaffold request."""

    unit_id: str
    display_name: str
    branch: str = "main"
    package_owner: Literal["space-ops-platform", "space-ops-apps"] | None = None
    source_path: str | None = None
    discovery: dict[str, Any] = Field(default_factory=dict)


class TemplateSummary(BaseModel):
    """Template catalog summary."""

    template_id: str
    display_name: str
    unit_kind: Literal["service", "module"]
    allowed_package_owners: list[str]
    description: str


class RuntimeEndpointSummary(BaseModel):
    """Runtime endpoint summary for registry responses."""

    service_name: str
    host: str
    port: int
    proxy_base_path: str
    health_path: str


class RegistryUnitResponse(BaseModel):
    """Managed unit response."""

    unit_id: str
    display_name: str
    package_owner: str
    unit_kind: str
    runtime_template: str
    source_path: str
    active_deployment_id: str | None = None
    deployment_status: str
    health_status: str
    discovery_metadata_json: dict[str, Any]
    runtime_endpoint: RuntimeEndpointSummary | None = None


class DeploymentRecordResponse(BaseModel):
    """Deployment response."""

    deployment_id: str
    unit_id: str
    branch: str
    commit_sha: str
    status: str
    health_status: str
    logs_url: str
    registered: bool
    failure_reason: str | None = None
