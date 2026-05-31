"""Deployment API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.deployments.service import DeploymentService
from app.git.repository import ManagedGitRepository
from app.registry.service import RegistryService
from app.schemas import DeploymentRecordResponse, DeploymentSubmissionRequest

router = APIRouter(prefix="/deployments", tags=["deployments"])


def get_deployment_service(session: Session = Depends(get_db)) -> DeploymentService:
    settings = app.config.get_settings()
    return DeploymentService(settings, ManagedGitRepository(settings), session)


@router.post("", response_model=DeploymentRecordResponse)
def submit_deployment(
    request: DeploymentSubmissionRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> DeploymentRecordResponse:
    try:
        return service.submit(request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{deployment_id}", response_model=DeploymentRecordResponse)
def get_deployment(
    deployment_id: str,
    session: Session = Depends(get_db),
) -> DeploymentRecordResponse:
    registry = RegistryService(session)
    deployment = registry.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    return DeploymentRecordResponse(
        deployment_id=deployment.deployment_id,
        unit_id=deployment.unit_id,
        branch=deployment.branch,
        commit_sha=deployment.commit_sha,
        deployment_intent=deployment.deployment_intent,
        status=deployment.status,
        health_status=deployment.health_status,
        logs_url=f"/deployments/{deployment.deployment_id}/logs",
        registered=deployment.status == "healthy",
        failure_reason=deployment.failure_reason,
        validation_status=registry.summarize_validation_status(deployment.deployment_id),
        next_validation_steps=registry.suggested_validation_steps_for_deployment(deployment),
        success_claim_allowed=registry.success_claim_allowed(deployment),
    )


@router.get("/{deployment_id}/logs")
def get_logs(deployment_id: str, session: Session = Depends(get_db)) -> dict:
    registry = RegistryService(session)
    deployment = registry.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    settings = app.config.get_settings()
    return {"deployment_id": deployment_id, "logs": registry.read_logs(settings.deployment_logs_root, deployment_id)}
