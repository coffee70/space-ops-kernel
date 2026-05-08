"""Runtime bootstrap status API."""

from __future__ import annotations

from fastapi import APIRouter

from app.services.bootstrap_status import RuntimeBootstrapStatusService

router = APIRouter(prefix="/bootstrap", tags=["bootstrap"])


@router.get("/status")
def get_bootstrap_status() -> dict:
    return RuntimeBootstrapStatusService().get_latest_run_snapshot()
