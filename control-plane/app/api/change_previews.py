"""Chat-native change preview deploy/revert adapter.

Thin endpoint surface that lets the AI Engineer chat experience deploy
a preview branch and restore the baseline without re-implementing the
deployment service. Internally calls :class:`DeploymentService.submit` so
all registry, audit, and runtime invariants stay in one place.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.deployments.service import DeploymentService
from app.git.repository import ManagedGitRepository
from app.registry.service import RegistryService
from app.schemas import (
    ChangePreviewDeployRequest,
    ChangePreviewDeployResponse,
    ChangePreviewRevertRequest,
    ChangePreviewRevertResponse,
    DeploymentSubmissionRequest,
)


router = APIRouter(prefix="/change-previews", tags=["change-previews"])


def get_deployment_service(session: Session = Depends(get_db)) -> DeploymentService:
    settings = app.config.get_settings()
    return DeploymentService(settings, ManagedGitRepository(settings), session)


@router.post("/deploy", response_model=ChangePreviewDeployResponse)
def deploy_change_preview(
    request: ChangePreviewDeployRequest,
    service: DeploymentService = Depends(get_deployment_service),
) -> ChangePreviewDeployResponse:
    try:
        record = service.submit(
            DeploymentSubmissionRequest(
                unit_id=request.target_unit_id,
                branch=request.branch,
                commit_sha=request.commit_sha,
            )
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ChangePreviewDeployResponse(
        deployment_id=record.deployment_id,
        unit_id=record.unit_id,
        branch=record.branch,
        commit_sha=record.commit_sha,
        status=record.status,
        health_status=record.health_status,
        logs_url=record.logs_url,
        registered=record.registered,
        failure_reason=record.failure_reason,
        target_unit_id=request.target_unit_id,
        target_application_id=request.target_application_id,
        conversation_id=request.conversation_id,
        agent_run_id=request.agent_run_id,
    )


@router.post("/revert", response_model=ChangePreviewRevertResponse)
def revert_change_preview(
    request: ChangePreviewRevertRequest,
    service: DeploymentService = Depends(get_deployment_service),
    session: Session = Depends(get_db),
) -> ChangePreviewRevertResponse:
    registry = RegistryService(session)
    if request.preview_deployment_id and registry.get_deployment(request.preview_deployment_id) is None:
        raise HTTPException(status_code=404, detail="preview deployment not found")

    try:
        record = service.submit(
            DeploymentSubmissionRequest(
                unit_id=request.target_unit_id,
                branch=request.baseline_branch,
                commit_sha=request.baseline_commit_sha,
            )
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ChangePreviewRevertResponse(
        deployment_id=record.deployment_id,
        unit_id=record.unit_id,
        branch=record.branch,
        commit_sha=record.commit_sha,
        status=record.status,
        health_status=record.health_status,
        logs_url=record.logs_url,
        registered=record.registered,
        failure_reason=record.failure_reason,
        target_unit_id=request.target_unit_id,
        target_application_id=request.target_application_id,
        conversation_id=request.conversation_id,
        agent_run_id=request.agent_run_id,
        preview_deployment_id=request.preview_deployment_id,
    )
