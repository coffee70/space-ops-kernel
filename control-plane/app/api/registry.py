"""Registry API and module proxying."""

from __future__ import annotations

from typing import Iterable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.runtime import ManagedUnit
from app.registry.service import RegistryService
from app.schemas import RegistryUnitResponse

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


def _serialize_units(units: Iterable[ManagedUnit]) -> list[RegistryUnitResponse]:
    return [RegistryUnitResponse.model_validate(unit, from_attributes=True) for unit in units]


@router.get("/units", response_model=list[RegistryUnitResponse])
def get_units(session: Session = Depends(get_db)) -> list[RegistryUnitResponse]:
    return _serialize_units(RegistryService(session).get_units())


@router.get("/units/{unit_id}", response_model=RegistryUnitResponse)
def get_unit(unit_id: str, session: Session = Depends(get_db)) -> RegistryUnitResponse:
    unit = RegistryService(session).get_unit(unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="unit not found")
    return RegistryUnitResponse.model_validate(unit, from_attributes=True)


@router.get("/services", response_model=list[RegistryUnitResponse])
def get_services(session: Session = Depends(get_db)) -> list[RegistryUnitResponse]:
    return _serialize_units(RegistryService(session).get_units(kind="service"))


@router.get("/modules", response_model=list[RegistryUnitResponse])
def get_modules(session: Session = Depends(get_db)) -> list[RegistryUnitResponse]:
    return _serialize_units(RegistryService(session).get_units(kind="module"))


@proxy_router.api_route("/modules/{slug}", methods=["GET", "HEAD"])
@proxy_router.api_route("/modules/{slug}/{path:path}", methods=["GET", "HEAD"])
async def proxy_module(slug: str, request: Request, path: str = "", session: Session = Depends(get_db)) -> Response:
    units = RegistryService(session).get_units(kind="module")
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

    runtime_ref = unit.discovery_metadata_json.get("runtime_ref") or {}
    target_url = runtime_ref.get("target_url")
    if not target_url:
        raise HTTPException(status_code=502, detail="module has no target url")

    upstream = target_url.rstrip("/")
    if path:
        upstream = f"{upstream}/{path}"
    if request.url.query:
        upstream = f"{upstream}?{request.url.query}"

    headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        upstream_response = await client.request(request.method, upstream, headers=headers)

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers={key: value for key, value in upstream_response.headers.items() if key.lower() not in HOP_BY_HOP_HEADERS},
        media_type=upstream_response.headers.get("content-type"),
    )

