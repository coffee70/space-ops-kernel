"""Registry API and controlled runtime proxying."""

from __future__ import annotations

from typing import Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
    SERVICE_SLUG_PATTERN,
)
from app.services.proxy_targets import (
    RuntimeProxyValidationError,
    build_runtime_upstream_url,
    validate_runtime_path,
    validate_runtime_ref,
)

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


def _serialize_service(unit: ManagedUnit) -> RegistryServiceResponse:
    discovery = unit.discovery_metadata_json if isinstance(unit.discovery_metadata_json, dict) else {}
    service_slug = discovery.get("service_slug")
    if not isinstance(service_slug, str) or not SERVICE_SLUG_PATTERN.fullmatch(service_slug):
        service_slug = unit.unit_id
    return RegistryServiceResponse(
        serviceSlug=service_slug,
        unitId=unit.unit_id,
        displayName=unit.display_name,
        packageOwner=unit.package_owner,
        runtimeKind=unit.runtime_kind,
        runtimeTemplate=unit.runtime_template,
        deploymentStatus=unit.deployment_status,
        healthStatus=unit.health_status,
        category=_safe_discovery_text(discovery, "category"),
        description=_safe_discovery_text(discovery, "description"),
        capabilities=_safe_discovery_capabilities(discovery),
    )


def _serialize_services(units: Iterable[ManagedUnit]) -> list[RegistryServiceResponse]:
    return [_serialize_service(unit) for unit in units]


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
def get_services(session: Session = Depends(get_db)) -> list[RegistryServiceResponse]:
    registry = RegistryService(session)
    return _serialize_services(registry.get_units(kind="service"))


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
    return _serialize_service(unit)


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


async def _proxy_request(
    runtime_ref: RuntimeRef,
    request: Request,
    path: str = "",
    raw_path: str = "",
    strip_sensitive_headers: bool = False,
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
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
            upstream_response = await client.request(
                request.method,
                upstream,
                headers=headers,
                content=body if body else None,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="runtime proxy unavailable") from exc

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
    return await _proxy_request(runtime_ref, request, path=path, raw_path=raw_path)


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
    runtime_ref = _get_runtime_ref_for_unit(registry, unit)
    raw_path = _extract_raw_proxy_path(request, f"/internal/runtime-services/{service_slug}")
    return await _proxy_request(
        runtime_ref,
        request,
        path=path,
        raw_path=raw_path,
        strip_sensitive_headers=True,
    )
