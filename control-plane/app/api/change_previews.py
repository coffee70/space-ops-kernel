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
from app.models.runtime import ApplicationDeployment
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
    if request.preview_deployment_id:
        preview_deployment = registry.get_deployment(request.preview_deployment_id)
        if preview_deployment is None:
            raise HTTPException(status_code=404, detail="preview deployment not found")
        if preview_deployment.unit_id != request.target_unit_id:
            # The chat experience must not allow reverting deployment A while
            # claiming it restores unit B. Mismatch is a client-side correctness
            # bug, not a missing record, so 400 is the right status here.
            raise HTTPException(
                status_code=400,
                detail={
                    "error_code": "preview_deployment_unit_mismatch",
                    "message": "preview deployment does not belong to the requested target unit",
                    "preview_deployment_id": request.preview_deployment_id,
                    "preview_unit_id": preview_deployment.unit_id,
                    "requested_target_unit_id": request.target_unit_id,
                },
            )
        if request.target_application_id is not None:
            unit = registry.get_unit(preview_deployment.unit_id)
            if unit is not None and unit.runtime_kind == "frontend_application":
                application_deployment = (
                    session.get(ApplicationDeployment, preview_deployment.deployment_id)
                )
                if (
                    application_deployment is not None
                    and application_deployment.application_id != request.target_application_id
                ):
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error_code": "preview_deployment_application_mismatch",
                            "message": "preview deployment does not belong to the requested target application",
                            "preview_deployment_id": request.preview_deployment_id,
                            "preview_application_id": application_deployment.application_id,
                            "requested_target_application_id": request.target_application_id,
                        },
                    )

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
