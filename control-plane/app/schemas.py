"""Pydantic schemas."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

APPLICATION_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LOADER_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SERVICE_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SAFE_COLOR_PATTERN = re.compile(
    r"^(?:#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}(?:\s*,\s*(?:0|1|0?\.\d+))?\s*\))$"
)
SAFE_IFRAME_SANDBOX_TOKENS = {
    "allow-downloads",
    "allow-forms",
    "allow-modals",
    "allow-pointer-lock",
    "allow-popups",
    "allow-popups-to-escape-sandbox",
    "allow-presentation",
    "allow-same-origin",
    "allow-scripts",
    "allow-storage-access-by-user-activation",
    "allow-top-navigation-by-user-activation",
}
SAFE_IFRAME_ALLOW_TOKENS = {
    "clipboard-read",
    "clipboard-write",
    "fullscreen",
}
RESERVED_SHELL_ROUTES = {"/api", "/apps", "/platform", "/runtime-applications", "/workspace"}


def _reject_unsafe_path(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    if len(value) > 255:
        raise ValueError(f"{field_name} exceeds maximum length")
    if not value.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute same-origin path")
    lowered = value.lower()
    if "//" in value or "\\\\" in value:
        raise ValueError(f"{field_name} must not contain repeated slashes")
    if any(token in lowered for token in ("..", "%2f", "%5c", "http://", "https://", "//")):
        raise ValueError(f"{field_name} must be a normalized same-origin path")
    return value


def _validate_application_id(value: str) -> str:
    if len(value) > 64:
        raise ValueError("application_id exceeds maximum length")
    if not APPLICATION_ID_PATTERN.fullmatch(value):
        raise ValueError("application_id must be a lowercase slug")
    return value


def _validate_loader_key(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 64:
        raise ValueError("loader_key exceeds maximum length")
    if not LOADER_KEY_PATTERN.fullmatch(value):
        raise ValueError("loader_key must be a lowercase slug")
    return value


def _validate_safe_color(value: str, *, field_name: str) -> str:
    if len(value) > 64:
        raise ValueError(f"{field_name} exceeds maximum length")
    if not SAFE_COLOR_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe CSS color literal")
    return value


def _validate_iframe_sandbox(value: str | None) -> str | None:
    if value in (None, ""):
        return None if value is None else ""
    tokens = value.split()
    if not tokens:
        return ""
    invalid = sorted(token for token in tokens if token not in SAFE_IFRAME_SANDBOX_TOKENS)
    if invalid:
        raise ValueError(f"iframe_sandbox contains unsupported tokens: {', '.join(invalid)}")
    return " ".join(tokens)


def _validate_iframe_allow(value: str | None) -> str | None:
    if value in (None, ""):
        return None if value is None else ""
    tokens = [token.strip() for token in value.split(";") if token.strip()]
    invalid = sorted(token for token in tokens if token not in SAFE_IFRAME_ALLOW_TOKENS)
    if invalid:
        raise ValueError(f"iframe_allow contains unsupported tokens: {', '.join(invalid)}")
    return "; ".join(tokens)


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Envelope(BaseModel):
    """Common code-operation metadata envelope."""

    branch: str
    commit_sha: str
    path: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


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


class BuildSpec(StrictBaseModel):
    """Build contract."""

    command: str


class RunSpec(StrictBaseModel):
    """Run contract."""

    command: str


class HealthSpec(StrictBaseModel):
    """Health contract."""

    type: Literal["http"]
    path: str
    port: int


class VolumeMountSpec(StrictBaseModel):
    """Host bind mount relative to the Space Ops workspace root."""

    source: str = Field(..., min_length=1, max_length=1024)
    target: str = Field(..., min_length=1, max_length=1024)
    read_only: bool = True

    @field_validator("source")
    @classmethod
    def validate_source_workspace_relative(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("mount source cannot be empty")
        path = PurePosixPath(trimmed)
        if path.is_absolute():
            raise ValueError("mount source must be relative to workspace_root")
        if ".." in path.parts:
            raise ValueError("mount source must not contain '..'")
        return path.as_posix()

    @field_validator("target")
    @classmethod
    def validate_target_container_path(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("mount target cannot be empty")
        path = PurePosixPath(trimmed)
        if not path.is_absolute():
            raise ValueError("mount target must be an absolute container path")
        if ".." in path.parts:
            raise ValueError("mount target must not contain '..'")
        return path.as_posix()


class RuntimeTransport(StrictBaseModel):
    """Transport settings for an active runtime."""

    scheme: Literal["http"] = "http"
    host: str
    port: int


class RuntimeHealth(StrictBaseModel):
    """Health endpoint settings for an active runtime."""

    path: str


class RuntimeProxy(StrictBaseModel):
    """Proxy path settings for an active runtime."""

    base_path: str = ""


class RuntimeRef(StrictBaseModel):
    """Structured runtime connectivity metadata."""

    service_name: str
    compose_file: str | None = None
    env_file: str | None = None
    transport: RuntimeTransport
    health: RuntimeHealth
    proxy: RuntimeProxy = Field(default_factory=RuntimeProxy)


PlatformApplicationType = Literal["native", "embedded"]
RuntimeKind = Literal["service", "frontend_application", "frontend_shell"]
RuntimeTemplate = Literal[
    "python-service",
    "node-service",
    "frontend-native-application",
    "frontend-embedded-application",
    "frontend-shell",
]


class PlatformApplicationDefinition(StrictBaseModel):
    """Public registry record for a platform application."""

    application_id: str = Field(alias="applicationId")
    title: str = Field(min_length=1, max_length=120, alias="title")
    description: str = Field(min_length=1, max_length=320, alias="description")
    icon_key: str = Field(min_length=1, max_length=64, alias="iconKey")
    icon_color: str = Field(alias="iconColor")
    icon_background: str = Field(alias="iconBackground")
    application_type: PlatformApplicationType = Field(alias="applicationType")
    route_path: str = Field(alias="routePath")
    loader_key: str | None = Field(default=None, alias="loaderKey")
    embedded_url: str | None = Field(default=None, alias="embeddedUrl")
    proxy_base_path: str | None = Field(default=None, alias="proxyBasePath")
    version: str = Field(min_length=1, max_length=64, alias="version")
    enabled: bool = True
    iframe_sandbox: str | None = Field(default=None, alias="iframeSandbox")
    iframe_allow: str | None = Field(default=None, alias="iframeAllow")
    sort_order: int = Field(default=100, ge=0, le=10000, alias="sortOrder")
    owner: str | None = Field(default=None, max_length=120)
    capabilities: list[str] = Field(default_factory=list)
    health_status: str = Field(default="unknown", min_length=1, max_length=64, alias="healthStatus")
    deployment_status: str = Field(default="seeded", min_length=1, max_length=64, alias="deploymentStatus")

    @field_validator("application_id")
    @classmethod
    def validate_application_id(cls, value: str) -> str:
        return _validate_application_id(value)

    @field_validator("loader_key")
    @classmethod
    def validate_loader_key(cls, value: str | None) -> str | None:
        return _validate_loader_key(value)

    @field_validator("icon_color")
    @classmethod
    def validate_icon_color(cls, value: str) -> str:
        return _validate_safe_color(value, field_name="icon_color")

    @field_validator("icon_background")
    @classmethod
    def validate_icon_background(cls, value: str) -> str:
        return _validate_safe_color(value, field_name="icon_background")

    @field_validator("route_path")
    @classmethod
    def validate_route_path(cls, value: str) -> str:
        return _reject_unsafe_path(value, field_name="route_path") or value

    @field_validator("embedded_url")
    @classmethod
    def validate_embedded_url(cls, value: str | None) -> str | None:
        return _reject_unsafe_path(value, field_name="embedded_url")

    @field_validator("proxy_base_path")
    @classmethod
    def validate_proxy_base_path(cls, value: str | None) -> str | None:
        return _reject_unsafe_path(value, field_name="proxy_base_path")

    @field_validator("iframe_sandbox")
    @classmethod
    def validate_iframe_sandbox(cls, value: str | None) -> str | None:
        return _validate_iframe_sandbox(value)

    @field_validator("iframe_allow")
    @classmethod
    def validate_iframe_allow(cls, value: str | None) -> str | None:
        return _validate_iframe_allow(value)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        sanitized: list[str] = []
        for capability in value:
            if not isinstance(capability, str):
                raise ValueError("capabilities must contain only strings")
            trimmed = capability.strip()
            if not trimmed:
                raise ValueError("capabilities must not contain empty values")
            if len(trimmed) > 64:
                raise ValueError("capability exceeds maximum length")
            if trimmed in seen:
                continue
            seen.add(trimmed)
            sanitized.append(trimmed)
        return sanitized

    @model_validator(mode="after")
    def validate_application_contract(self) -> "PlatformApplicationDefinition":
        expected_base_route = f"/apps/{self.application_id}"
        if self.route_path != expected_base_route:
            raise ValueError("route_path must equal /apps/:applicationId")
        if self.route_path in RESERVED_SHELL_ROUTES:
            raise ValueError("route_path uses a reserved shell route")

        if self.application_type == "native":
            if not self.loader_key:
                raise ValueError("native applications require loader_key")
            if self.embedded_url or self.proxy_base_path:
                raise ValueError("native applications cannot define embedded transport fields")
        else:
            if self.loader_key:
                raise ValueError("embedded applications cannot define loader_key")
            if not (self.embedded_url or self.proxy_base_path):
                raise ValueError("embedded applications require embedded_url or proxy_base_path")
        return self


class SeededApplicationDefinition(PlatformApplicationDefinition):
    """Strict seed/application manifest payload."""


class ApplicationManifestDefinition(StrictBaseModel):
    """Frontend application metadata embedded in deployment manifests."""

    application_id: str
    title: str
    description: str
    icon_key: str
    icon_color: str
    icon_background: str
    application_type: PlatformApplicationType
    route_path: str
    loader_key: str | None = None
    embedded_url: str | None = None
    proxy_base_path: str | None = None
    version: str
    enabled: bool = True
    iframe_sandbox: str | None = None
    iframe_allow: str | None = None
    sort_order: int = Field(default=100, ge=0, le=10000)
    owner: str | None = Field(default=None, max_length=120)
    capabilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_against_public_contract(self) -> "ApplicationManifestDefinition":
        PlatformApplicationDefinition.model_validate(
            {
                "applicationId": self.application_id,
                "title": self.title,
                "description": self.description,
                "iconKey": self.icon_key,
                "iconColor": self.icon_color,
                "iconBackground": self.icon_background,
                "applicationType": self.application_type,
                "routePath": self.route_path,
                "loaderKey": self.loader_key,
                "embeddedUrl": self.embedded_url,
                "proxyBasePath": self.proxy_base_path,
                "version": self.version,
                "enabled": self.enabled,
                "iframeSandbox": self.iframe_sandbox,
                "iframeAllow": self.iframe_allow,
                "sortOrder": self.sort_order,
                "owner": self.owner,
                "capabilities": self.capabilities,
                "healthStatus": "unknown",
                "deploymentStatus": "seeded",
            }
        )
        return self


class UnitManifest(StrictBaseModel):
    """Managed runtime manifest."""

    unit_id: str
    display_name: str
    package_owner: Literal["space-ops-platform", "space-ops-apps"]
    runtime_kind: RuntimeKind
    runtime_template: RuntimeTemplate
    source_path: str
    build: BuildSpec
    run: RunSpec
    health: HealthSpec
    discovery: dict[str, Any] = Field(default_factory=dict)
    application: ApplicationManifestDefinition | None = None
    mounts: list[VolumeMountSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_runtime_contract(self) -> "UnitManifest":
        if self.runtime_kind == "service":
            if self.application is not None:
                raise ValueError("service manifests cannot define application metadata")
            if self.runtime_template not in {"python-service", "node-service"}:
                raise ValueError("service manifests must use a service runtime_template")
            return self

        if self.runtime_kind == "frontend_application":
            if self.application is None:
                raise ValueError("frontend_application manifests require application metadata")
            if self.runtime_template == "frontend-native-application" and self.application.application_type != "native":
                raise ValueError("frontend-native-application requires application_type=native")
            if self.runtime_template == "frontend-embedded-application" and self.application.application_type != "embedded":
                raise ValueError("frontend-embedded-application requires application_type=embedded")
            if self.runtime_template not in {"frontend-native-application", "frontend-embedded-application"}:
                raise ValueError("frontend_application manifests must use an application runtime_template")
            return self

        if self.runtime_kind == "frontend_shell":
            if self.runtime_template != "frontend-shell":
                raise ValueError("frontend_shell manifests must use runtime_template=frontend-shell")
        return self


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
    runtime_kind: RuntimeKind
    allowed_package_owners: list[str]
    description: str


class RegistryServiceResponse(BaseModel):
    """Public service catalog response without runtime topology."""

    model_config = ConfigDict(populate_by_name=True)

    service_slug: str = Field(alias="serviceSlug")
    unit_id: str = Field(alias="unitId")
    display_name: str = Field(alias="displayName")
    package_owner: str = Field(alias="packageOwner")
    runtime_kind: str = Field(alias="runtimeKind")
    runtime_template: str = Field(alias="runtimeTemplate")
    deployment_status: str = Field(alias="deploymentStatus")
    health_status: str = Field(alias="healthStatus")
    category: str | None = None
    description: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("service_slug")
    @classmethod
    def validate_service_slug(cls, value: str) -> str:
        if len(value) > 64:
            raise ValueError("service_slug exceeds maximum length")
        if not SERVICE_SLUG_PATTERN.fullmatch(value):
            raise ValueError("service_slug must be a lowercase slug")
        return value

    @field_validator("category", "description")
    @classmethod
    def validate_safe_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        if not trimmed:
            return None
        if len(trimmed) > 320:
            raise ValueError("text field exceeds maximum length")
        return trimmed

    @field_validator("capabilities")
    @classmethod
    def validate_service_capabilities(cls, value: list[str]) -> list[str]:
        return PlatformApplicationDefinition.validate_capabilities(value)


class RegistryUnitSummaryResponse(BaseModel):
    """Public unit catalog response without runtime/deployment internals."""

    model_config = ConfigDict(populate_by_name=True)

    unit_id: str = Field(alias="unitId")
    display_name: str = Field(alias="displayName")
    package_owner: str = Field(alias="packageOwner")
    runtime_kind: str = Field(alias="runtimeKind")
    runtime_template: str = Field(alias="runtimeTemplate")
    deployment_status: str = Field(alias="deploymentStatus")
    health_status: str = Field(alias="healthStatus")
    service_slug: str | None = Field(default=None, alias="serviceSlug")
    application_id: str | None = Field(default=None, alias="applicationId")
    category: str | None = None
    description: str | None = None

    @field_validator("service_slug")
    @classmethod
    def validate_optional_service_slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return RegistryServiceResponse.validate_service_slug(value)

    @field_validator("application_id")
    @classmethod
    def validate_optional_application_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_application_id(value)

    @field_validator("category", "description")
    @classmethod
    def validate_safe_optional_text(cls, value: str | None) -> str | None:
        return RegistryServiceResponse.validate_safe_optional_text(value)


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
