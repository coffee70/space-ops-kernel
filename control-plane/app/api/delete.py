"""Managed resource delete routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.delete.schemas import DeleteCodeRequest, DeleteManagedUnitRequest, DeleteReport, DeleteStaleRequest
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


@router.post("/stale", response_model=DeleteReport)
def delete_stale(
    request: DeleteStaleRequest,
    service: ManagedResourceDeleteService = Depends(get_delete_service),
) -> DeleteReport:
    try:
        return service.delete_stale(request)
    except DeleteValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
