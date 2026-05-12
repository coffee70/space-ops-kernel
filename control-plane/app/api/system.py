"""Read-only system status APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.schemas import SystemDeploymentOverviewResponse
from app.services.system_status import SystemStatusService

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/deployments/overview", response_model=SystemDeploymentOverviewResponse)
def get_deployments_overview(session: Session = Depends(get_db)) -> SystemDeploymentOverviewResponse:
    """Return a read-only projection of core and runtime deployment status."""

    return SystemStatusService(app.config.get_settings(), session).build_overview()
