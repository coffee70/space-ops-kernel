"""Registry API and controlled runtime proxying."""

from __future__ import annotations

import logging
import time
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.runtime import ManagedUnit
from app.registry.service import RegistryService
from app.schemas import (
    APPLICATION_ID_PATTERN,
    PlatformApplicationDefinition,
    RegistryServiceResponse,
    RegistryUnitSummaryResponse,
    RuntimeRef,
    RuntimeTransportDebug,
    SERVICE_SLUG_PATTERN,
)
from app.services.proxy_targets import (
    RuntimeProxyValidationError,
    build_runtime_upstream_url,
    validate_runtime_path,
    validate_runtime_ref,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/registry", tags=["registry"])
proxy_router = APIRouter(prefix="/runtime-applications", tags=["runtime-applications"])
internal_proxy_router = APIRouter(prefix="/internal/runtime-services", tags=["internal-runtime-services"])

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
SENSITIVE_FORWARD_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-forwarded-user",
    "x-forwarded-email",
    "x-forwarded-access-token",
}


def _safe_discovery_text(discovery: dict, field: str) -> str | None:
    value = discovery.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 320:
        return None
    return value


def _safe_discovery_capabilities(discovery: dict) -> list[str]:
    capabilities = discovery.get("capabilities")
    if not isinstance(capabilities, list):
        capabilities = discovery.get("capability_tags")
    if not isinstance(capabilities, list):
        return []
    safe: list[str] = []
    seen: set[str] = set()
    for capability in capabilities:
        if not isinstance(capability, str):
            continue
        value = capability.strip()
        if not value or len(value) > 64 or value in seen:
            continue
        seen.add(value)
        safe.append(value)
    return safe


def _active_deployment_branch_commit(registry: RegistryService, unit_id: str) -> tuple[str | None, str | None]:
    deployment = registry.get_active_deployment_for_unit(unit_id)
    if deployment is None:
        deployment = registry.get_latest_deployment_for_unit(unit_id)
    if deployment is None:
        return None, None
    return deployment.branch, deployment.commit_sha


def _serialize_service(
    unit: ManagedUnit,
    *,
    registry: RegistryService | None = None,
    runtime_target: RuntimeTransportDebug | None = None,
) -> RegistryServiceResponse:
    discovery = unit.discovery_metadata_json if isinstance(unit.discovery_metadata_json, dict) else {}
    service_slug = discovery.get("service_slug")
    if not isinstance(service_slug, str) or not SERVICE_SLUG_PATTERN.fullmatch(service_slug):
        service_slug = unit.unit_id
    branch: str | None = None
    commit_sha: str | None = None
    if registry is not None:
        branch, commit_sha = _active_deployment_branch_commit(registry, unit.unit_id)
    return RegistryServiceResponse(
        serviceSlug=service_slug,
        unitId=unit.unit_id,
        displayName=unit.display_name,
        packageOwner=unit.package_owner,
        runtimeKind=unit.runtime_kind,
        runtimeTemplate=unit.runtime_template,
        deploymentStatus=unit.deployment_status,
        healthStatus=unit.health_status,
        branch=branch,
        commitSha=commit_sha,
        category=_safe_discovery_text(discovery, "category"),
        description=_safe_discovery_text(discovery, "description"),
        capabilities=_safe_discovery_capabilities(discovery),
        runtime_target=runtime_target,
    )


def _serialize_services(units: Iterable[ManagedUnit], registry: RegistryService) -> list[RegistryServiceResponse]:
    return [_serialize_service(unit, registry=registry) for unit in units]


def _serialize_unit_summary(unit: ManagedUnit, registry: RegistryService) -> RegistryUnitSummaryResponse:
    discovery = unit.discovery_metadata_json if isinstance(unit.discovery_metadata_json, dict) else {}
    service_slug = discovery.get("service_slug") if unit.runtime_kind == "service" else None
    if not isinstance(service_slug, str) or not SERVICE_SLUG_PATTERN.fullmatch(service_slug):
        service_slug = None
    application_id = None
    if unit.runtime_kind == "frontend_application":
        for application in registry.get_applications():
            deployment = registry.get_active_application_deployment(application.application_id)
            if deployment is not None and deployment.deployment_id == unit.active_deployment_id:
                application_id = application.application_id
                break
    return RegistryUnitSummaryResponse(
        unit_id=unit.unit_id,
        display_name=unit.display_name,
        package_owner=unit.package_owner,
        runtime_kind=unit.runtime_kind,
        runtime_template=unit.runtime_template,
        deployment_status=unit.deployment_status,
        health_status=unit.health_status,
        service_slug=service_slug,
        application_id=application_id,
        category=_safe_discovery_text(discovery, "category"),
        description=_safe_discovery_text(discovery, "description"),
    )


def _serialize_unit_summaries(units: Iterable[ManagedUnit], registry: RegistryService) -> list[RegistryUnitSummaryResponse]:
    return [_serialize_unit_summary(unit, registry) for unit in units]


@router.get("/applications", response_model=list[PlatformApplicationDefinition])
def get_applications(session: Session = Depends(get_db)) -> list[PlatformApplicationDefinition]:
    registry = RegistryService(session)
    return [registry.serialize_application(application) for application in registry.get_applications()]


@router.get("/applications/{application_id}", response_model=PlatformApplicationDefinition)
def get_application(application_id: str, session: Session = Depends(get_db)) -> PlatformApplicationDefinition:
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise HTTPException(status_code=404, detail="application not found")
    registry = RegistryService(session)
    application = registry.get_application(application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    return registry.serialize_application(application)


def _toggle_application_enabled_state(
    application_id: str,
    *,
    enabled: bool,
    session: Session,
) -> PlatformApplicationDefinition:
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise HTTPException(status_code=404, detail="application not found")
    registry = RegistryService(session)
    definition = (
        registry.enable_application(application_id)
        if enabled
        else registry.disable_application(application_id)
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="application not found")
    return definition


@router.post("/applications/{application_id}/enable", response_model=PlatformApplicationDefinition)
def enable_application(application_id: str, session: Session = Depends(get_db)) -> PlatformApplicationDefinition:
    return _toggle_application_enabled_state(application_id, enabled=True, session=session)


@router.post("/applications/{application_id}/disable", response_model=PlatformApplicationDefinition)
def disable_application(application_id: str, session: Session = Depends(get_db)) -> PlatformApplicationDefinition:
    return _toggle_application_enabled_state(application_id, enabled=False, session=session)


@router.get("/units", response_model=list[RegistryUnitSummaryResponse])
def get_units(session: Session = Depends(get_db)) -> list[RegistryUnitSummaryResponse]:
    registry = RegistryService(session)
    return _serialize_unit_summaries(registry.get_units(), registry)


@router.get("/services", response_model=list[RegistryServiceResponse])
def get_services(
    session: Session = Depends(get_db),
    include_runtime_transport: bool = Query(False, alias="includeRuntimeTransport"),
) -> list[RegistryServiceResponse]:
    registry = RegistryService(session)
    settings = get_settings()
    if include_runtime_transport and settings.environment == "production":
        raise HTTPException(status_code=400, detail="includeRuntimeTransport is not allowed in production")
    units = registry.get_units(kind="service")
    if not include_runtime_transport:
        return _serialize_services(units, registry)

    payloads: list[RegistryServiceResponse] = []
    for unit in units:
        runtime_target: RuntimeTransportDebug | None = None
        try:
            ref = registry.get_runtime_ref_for_unit(unit.unit_id)
            if ref is not None:
                runtime_target = RuntimeTransportDebug(
                    serviceName=ref.service_name,
                    host=ref.transport.host,
                    port=ref.transport.port,
                    healthPath=ref.health.path,
                )
        except Exception:
            runtime_target = None
        payloads.append(_serialize_service(unit, registry=registry, runtime_target=runtime_target))
    return payloads


def _get_runtime_ref_for_unit(registry: RegistryService, unit: ManagedUnit) -> RuntimeRef:
    try:
        runtime_ref = registry.get_runtime_ref_for_unit(unit.unit_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="service has invalid runtime metadata") from exc
    if runtime_ref is None:
        raise HTTPException(status_code=502, detail="service has no active runtime")
    try:
        validate_runtime_ref(get_settings(), runtime_ref)
    except RuntimeProxyValidationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return runtime_ref


def _runtime_service_not_ready_response(
    service_slug: str,
    *,
    deployment_id: str | None = None,
    status: str | None = None,
) -> JSONResponse:
    payload = {
        "error_code": "runtime_service_not_ready",
        "service_slug": service_slug,
        "message": f"Runtime service is not ready: {service_slug}",
    }
    if deployment_id is not None:
        payload["deployment_id"] = deployment_id
    if status is not None:
        payload["status"] = status
    return JSONResponse(status_code=503, content=payload)


def _service_not_ready_payload(registry: RegistryService, unit: ManagedUnit) -> dict[str, str | None] | None:
    active = registry.get_active_deployment_for_unit(unit.unit_id)
    if active is not None and active.runtime_ref:
        return None
    deployment = registry.get_deployment(unit.active_deployment_id) if unit.active_deployment_id else None
    if deployment is None:
        deployment = registry.get_latest_deployment_for_unit(unit.unit_id)
    return {
        "deployment_id": deployment.deployment_id if deployment is not None else None,
        "status": deployment.status if deployment is not None else unit.deployment_status,
    }


def _find_service_by_slug(units: Iterable[ManagedUnit], service_slug: str) -> ManagedUnit | None:
    return next(
        (
            candidate
            for candidate in units
            if candidate.discovery_metadata_json.get("service_slug") == service_slug
        ),
        None,
    )


@router.get("/services/{service_slug}", response_model=RegistryServiceResponse)
def get_service(service_slug: str, session: Session = Depends(get_db)) -> RegistryServiceResponse:
    if not SERVICE_SLUG_PATTERN.fullmatch(service_slug):
        raise HTTPException(status_code=404, detail="service not found")
    registry = RegistryService(session)
    unit = _find_service_by_slug(registry.get_units(kind="service"), service_slug)
    if unit is None:
        raise HTTPException(status_code=404, detail="service not found")
    return _serialize_service(unit, registry=registry)


def _get_runtime_ref_for_application(registry: RegistryService, application_id: str) -> RuntimeRef:
    application = registry.get_application(application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    if not application.proxy_base_path:
        raise HTTPException(status_code=404, detail="application has no proxy transport")
    try:
        runtime_ref = registry.get_runtime_ref_for_application(application_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="application has invalid runtime metadata") from exc
    if runtime_ref is None:
        raise HTTPException(status_code=502, detail="application has no active runtime")
    try:
        validate_runtime_ref(get_settings(), runtime_ref)
    except RuntimeProxyValidationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return runtime_ref


def _extract_raw_proxy_path(request: Request, route_prefix: str) -> str:
    raw_path = request.scope.get("raw_path")
    if not raw_path:
        return ""
    decoded_raw_path = raw_path.decode("latin-1")
    if decoded_raw_path == route_prefix:
        return ""
    if decoded_raw_path.startswith(f"{route_prefix}/"):
        return decoded_raw_path[len(route_prefix) + 1 :]
    return ""


def _upstream_for_log(url: str) -> str:
    """Log scheme/host/port/path without query strings (may contain opaque ids)."""

    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "", "", "", ""))


async def _proxy_request(
    runtime_ref: RuntimeRef,
    request: Request,
    path: str = "",
    raw_path: str = "",
    strip_sensitive_headers: bool = False,
    *,
    proxy_target_label: str | None = None,
    read_timeout_seconds: float | None = None,
) -> Response:
    try:
        validated_path = validate_runtime_path(path)
        if raw_path:
            validate_runtime_path(raw_path)
    except RuntimeProxyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    upstream = build_runtime_upstream_url(runtime_ref, path=validated_path, query=request.url.query)
    stripped_headers = HOP_BY_HOP_HEADERS | (SENSITIVE_FORWARD_HEADERS if strip_sensitive_headers else set())
    headers = {key: value for key, value in request.headers.items() if key.lower() not in stripped_headers}
    body = await request.body()
    settings = get_settings()
    read_seconds = settings.runtime_proxy_read_timeout_seconds if read_timeout_seconds is None else read_timeout_seconds
    timeout = httpx.Timeout(
        connect=settings.runtime_proxy_connect_timeout_seconds,
        read=read_seconds,
        write=read_seconds,
        pool=2.0,
    )
    label = proxy_target_label or runtime_ref.service_name
    upstream_log = _upstream_for_log(upstream)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            upstream_response = await client.request(
                request.method,
                upstream,
                headers=headers,
                content=body if body else None,
            )
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ConnectTimeout) as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.error(
            "runtime proxy upstream timeout target=%s method=%s upstream=%s elapsed_ms=%s exc_type=%s msg=%s",
            label,
            request.method,
            upstream_log,
            elapsed_ms,
            type(exc).__name__,
            str(exc) or "(empty)",
        )
        raise HTTPException(
            status_code=504,
            detail="runtime proxy upstream timeout",
        ) from exc
    except httpx.ConnectError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.error(
            "runtime proxy upstream connect failed target=%s method=%s upstream=%s elapsed_ms=%s exc_type=%s msg=%s",
            label,
            request.method,
            upstream_log,
            elapsed_ms,
            type(exc).__name__,
            str(exc) or "(empty)",
        )
        raise HTTPException(
            status_code=502,
            detail="runtime proxy upstream connect failed",
        ) from exc
    except httpx.RequestError as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.error(
            "runtime proxy upstream error target=%s method=%s upstream=%s elapsed_ms=%s exc_type=%s msg=%s",
            label,
            request.method,
            upstream_log,
            elapsed_ms,
            type(exc).__name__,
            str(exc) or "(empty)",
        )
        raise HTTPException(
            status_code=502,
            detail="runtime proxy upstream transport error",
        ) from exc

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers={
            key: value
            for key, value in upstream_response.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        },
        media_type=upstream_response.headers.get("content-type"),
    )


@proxy_router.api_route(
    "/{application_id}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
@proxy_router.api_route(
    "/{application_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
async def proxy_application(
    application_id: str,
    request: Request,
    path: str = "",
    session: Session = Depends(get_db),
) -> Response:
    if not APPLICATION_ID_PATTERN.fullmatch(application_id):
        raise HTTPException(status_code=404, detail="application not found")
    registry = RegistryService(session)
    runtime_ref = _get_runtime_ref_for_application(registry, application_id)
    raw_path = _extract_raw_proxy_path(request, f"/runtime-applications/{application_id}")
    return await _proxy_request(
        runtime_ref,
        request,
        path=path,
        raw_path=raw_path,
        proxy_target_label=f"application:{application_id}",
    )


@internal_proxy_router.api_route(
    "/{service_slug}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
@internal_proxy_router.api_route(
    "/{service_slug}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
async def proxy_runtime_service(
    service_slug: str,
    request: Request,
    path: str = "",
    session: Session = Depends(get_db),
) -> Response:
    if not SERVICE_SLUG_PATTERN.fullmatch(service_slug):
        raise HTTPException(status_code=404, detail="service not found")
    registry = RegistryService(session)
    unit = _find_service_by_slug(registry.get_units(kind="service"), service_slug)
    if unit is None:
        raise HTTPException(status_code=404, detail="service not found")
    not_ready = _service_not_ready_payload(registry, unit)
    if not_ready is not None:
        return _runtime_service_not_ready_response(
            service_slug,
            deployment_id=not_ready.get("deployment_id"),
            status=not_ready.get("status"),
        )
    runtime_ref = _get_runtime_ref_for_unit(registry, unit)
    raw_path = _extract_raw_proxy_path(request, f"/internal/runtime-services/{service_slug}")
    settings = get_settings()
    normalized_path = (path or "").strip().strip("/")
    read_override: float | None = None
    if service_slug == "agent-runtime-service" and normalized_path == "chat" and request.method.upper() == "POST":
        read_override = max(settings.runtime_proxy_read_timeout_seconds, 300.0)
    return await _proxy_request(
        runtime_ref,
        request,
        path=path,
        raw_path=raw_path,
        strip_sensitive_headers=True,
        proxy_target_label=f"service:{service_slug}",
        read_timeout_seconds=read_override,
    )
