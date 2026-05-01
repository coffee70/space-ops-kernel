"""Managed resource delete routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.delete.schemas import DeleteCodeRequest, DeleteManagedUnitRequest, DeleteReport, DeleteScopeRequest, DeleteStaleRequest
from app.delete.service import DeleteValidationError, ManagedResourceDeleteService
from app.git.repository import ManagedGitRepository

router = APIRouter(prefix="/internal/delete", tags=["delete"])


def get_delete_service(session: Session = Depends(get_db)) -> ManagedResourceDeleteService:
    settings = app.config.get_settings()
    return ManagedResourceDeleteService(settings, ManagedGitRepository(settings), session)


@router.post("/managed-units", response_model=DeleteReport)
def delete_managed_unit(
    request: DeleteManagedUnitRequest,
    service: ManagedResourceDeleteService = Depends(get_delete_service),
) -> DeleteReport:
    try:
        return service.delete_managed_unit(request)
    except DeleteValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/code", response_model=DeleteReport)
def delete_code(
    request: DeleteCodeRequest,
    service: ManagedResourceDeleteService = Depends(get_delete_service),
) -> DeleteReport:
    try:
        return service.delete_code(request)
    except DeleteValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/scopes/{delete_scope_id}", response_model=DeleteReport)
def delete_scope(
    delete_scope_id: str,
    request: DeleteScopeRequest,
    service: ManagedResourceDeleteService = Depends(get_delete_service),
) -> DeleteReport:
    stale_request = DeleteStaleRequest(
        older_than_minutes=request.older_than_minutes or 1,
        include_code=request.include_code,
        include_runtime=request.include_runtime,
        include_registry=request.include_registry,
        include_intelligence_records=request.include_intelligence_records,
        request_id=request.request_id,
        agent_run_id=request.agent_run_id,
        tool_call_id=request.tool_call_id,
        conversation_id=request.conversation_id,
    )
    return service.delete_stale(stale_request, delete_scope_id=delete_scope_id)


@router.post("/stale", response_model=DeleteReport)
def delete_stale(
    request: DeleteStaleRequest,
    service: ManagedResourceDeleteService = Depends(get_delete_service),
) -> DeleteReport:
    try:
        return service.delete_stale(request)
    except DeleteValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
