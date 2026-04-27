"""Template API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.config import get_settings
from app.git.repository import ManagedGitRepository
from app.schemas import Envelope, ScaffoldRequest, TemplateSummary
from app.services.template_service import TemplateService

router = APIRouter(prefix="/templates", tags=["templates"])


def get_template_service() -> TemplateService:
    settings = get_settings()
    return TemplateService(settings, ManagedGitRepository(settings))


@router.get("", response_model=list[TemplateSummary])
def list_templates(service: TemplateService = Depends(get_template_service)) -> list[TemplateSummary]:
    return [TemplateSummary.model_validate(item) for item in service.list_templates()]


@router.get("/{template_id}")
def get_template(template_id: str, service: TemplateService = Depends(get_template_service)) -> dict:
    try:
        return service.get_template(template_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/{template_id}/scaffold", response_model=Envelope)
def scaffold(
    template_id: str,
    request: ScaffoldRequest,
    service: TemplateService = Depends(get_template_service),
) -> Envelope:
    try:
        result = service.scaffold(template_id, request)
        return Envelope(
            branch=result["branch"],
            commit_sha=result["commit_sha"],
            path=result["path"],
            changed_files=result["changed_files"],
            data={"manifest": result["manifest"]},
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
