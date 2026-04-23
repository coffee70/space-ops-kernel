"""Registry API and unit proxying."""

from __future__ import annotations

from typing import Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.runtime import ManagedUnit
from app.registry.service import RegistryService
from app.schemas import RegistryUnitResponse, RuntimeRef
from app.services.proxy_targets import (
    RuntimeProxyValidationError,
    build_runtime_upstream_url,
    runtime_endpoint_summary,
    validate_runtime_ref,
)

router = APIRouter(prefix="/registry", tags=["registry"])
proxy_router = APIRouter(prefix="/proxy", tags=["proxy"])

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


def _serialize_unit(unit: ManagedUnit, registry: RegistryService) -> RegistryUnitResponse:
    runtime_endpoint = None
    try:
        runtime_ref = registry.get_runtime_ref_for_unit(unit.unit_id)
    except Exception:
        runtime_ref = None
    if runtime_ref is not None:
        runtime_endpoint = runtime_endpoint_summary(runtime_ref)
    return RegistryUnitResponse(
        unit_id=unit.unit_id,
        display_name=unit.display_name,
        package_owner=unit.package_owner,
        unit_kind=unit.unit_kind,
        runtime_template=unit.runtime_template,
        source_path=unit.source_path,
        active_deployment_id=unit.active_deployment_id,
        deployment_status=unit.deployment_status,
        health_status=unit.health_status,
        discovery_metadata_json=unit.discovery_metadata_json,
        runtime_endpoint=runtime_endpoint,
    )


def _serialize_units(units: Iterable[ManagedUnit], registry: RegistryService) -> list[RegistryUnitResponse]:
    return [_serialize_unit(unit, registry) for unit in units]


@router.get("/units", response_model=list[RegistryUnitResponse])
def get_units(session: Session = Depends(get_db)) -> list[RegistryUnitResponse]:
    registry = RegistryService(session)
    return _serialize_units(registry.get_units(), registry)


@router.get("/units/{unit_id}", response_model=RegistryUnitResponse)
def get_unit(unit_id: str, session: Session = Depends(get_db)) -> RegistryUnitResponse:
    registry = RegistryService(session)
    unit = registry.get_unit(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")
    return _serialize_unit(unit, registry)


@router.get("/services", response_model=list[RegistryUnitResponse])
def get_services(session: Session = Depends(get_db)) -> list[RegistryUnitResponse]:
    registry = RegistryService(session)
    return _serialize_units(registry.get_units(kind="service"), registry)


@router.get("/modules", response_model=list[RegistryUnitResponse])
def get_modules(session: Session = Depends(get_db)) -> list[RegistryUnitResponse]:
    registry = RegistryService(session)
    return _serialize_units(registry.get_units(kind="module"), registry)


def _get_runtime_ref_for_unit(registry: RegistryService, unit: ManagedUnit) -> RuntimeRef:
    try:
        runtime_ref = registry.get_runtime_ref_for_unit(unit.unit_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="unit has invalid runtime metadata") from exc
    if runtime_ref is None:
        raise HTTPException(status_code=502, detail="unit has no active runtime")
    try:
        validate_runtime_ref(get_settings(), runtime_ref)
    except RuntimeProxyValidationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return runtime_ref


@proxy_router.api_route("/modules/{slug}", methods=["GET", "HEAD"])
@proxy_router.api_route("/modules/{slug}/{path:path}", methods=["GET", "HEAD"])
async def proxy_module(slug: str, request: Request, path: str = "", session: Session = Depends(get_db)) -> Response:
    registry = RegistryService(session)
    units = registry.get_units(kind="module")
    unit = next(
        (
            candidate
            for candidate in units
            if candidate.discovery_metadata_json.get("route_slug") == slug and candidate.active_deployment_id
        ),
        None,
    )
    if unit is None:
        raise HTTPException(status_code=404, detail="module not found")
    runtime_ref = _get_runtime_ref_for_unit(registry, unit)
    return await _proxy_request(runtime_ref, request, path=path)


def _find_service_by_slug(units: Iterable[ManagedUnit], service_slug: str) -> ManagedUnit | None:
    return next(
        (
            candidate
            for candidate in units
            if candidate.discovery_metadata_json.get("service_slug") == service_slug and candidate.active_deployment_id
        ),
        None,
    )


async def _proxy_request(runtime_ref: RuntimeRef, request: Request, path: str = "") -> Response:
    upstream = build_runtime_upstream_url(runtime_ref, path=path, query=request.url.query)
    headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}
    body = await request.body()
    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        upstream_response = await client.request(
            request.method,
            upstream,
            headers=headers,
            content=body if body else None,
        )

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
    "/services/{service_slug}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
@proxy_router.api_route(
    "/services/{service_slug}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
async def proxy_service(
    service_slug: str,
    request: Request,
    path: str = "",
    session: Session = Depends(get_db),
) -> Response:
    registry = RegistryService(session)
    units = registry.get_units(kind="service")
    unit = _find_service_by_slug(units, service_slug)
    if unit is None:
        raise HTTPException(status_code=404, detail="service not found")
    runtime_ref = _get_runtime_ref_for_unit(registry, unit)
    return await _proxy_request(runtime_ref, request, path=path)


@proxy_router.api_route(
    "/units/{unit_id}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
@proxy_router.api_route(
    "/units/{unit_id}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"],
)
async def proxy_unit(
    unit_id: str,
    request: Request,
    path: str = "",
    session: Session = Depends(get_db),
) -> Response:
    registry = RegistryService(session)
    unit = registry.get_unit(unit_id)
    if unit is None or not unit.active_deployment_id:
        raise HTTPException(status_code=404, detail="unit not found")
    runtime_ref = _get_runtime_ref_for_unit(registry, unit)
    return await _proxy_request(runtime_ref, request, path=path)
