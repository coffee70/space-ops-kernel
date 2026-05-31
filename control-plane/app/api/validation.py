"""Post-deploy validation APIs."""

from __future__ import annotations

from typing import Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import app.config
from app.db import get_db
from app.registry.service import RegistryService
from app.schemas import DeploymentValidationSummary, ValidationStep

ValidationStatus = Literal["not_run", "not_ready", "running", "passed", "failed", "partially_validated"]

router = APIRouter(prefix="/validation", tags=["validation"])


def _summary(registry: RegistryService, deployment_id: str) -> DeploymentValidationSummary:
    deployment = registry.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    attempts = registry.get_validation_attempts_for_deployment(deployment.deployment_id)
    latest_attempt = attempts[-1] if attempts else None
    checks = (
        registry.get_validation_checks_for_attempt(latest_attempt.id)
        if latest_attempt is not None
        else registry.get_validation_checks_for_deployment(deployment.deployment_id)
    )
    return DeploymentValidationSummary(
        deployment_id=deployment.deployment_id,
        unit_id=deployment.unit_id,
        validation_status=cast(ValidationStatus, registry.summarize_validation_status(deployment.deployment_id)),
        latest_attempt=registry.serialize_validation_attempt(latest_attempt) if latest_attempt is not None else None,
        attempts=[registry.serialize_validation_attempt(attempt) for attempt in attempts],
        checks=[registry.serialize_validation_check(check) for check in checks],
    )


def _body_contains(observed: Any, expected: dict[str, Any] | None) -> bool:
    if expected is None:
        return True
    if not isinstance(observed, dict):
        return False
    for key, expected_value in expected.items():
        if observed.get(key) != expected_value:
            return False
    return True


def _validation_base_url() -> str:
    settings = app.config.get_settings()
    edge_url = settings.frontend_api_server_url.rstrip("/")
    if edge_url:
        return edge_url
    return settings.frontend_control_plane_server_url.rstrip("/") or f"http://localhost:{settings.control_plane_port}"


async def _run_http_step(
    registry: RegistryService,
    *,
    attempt_id: str,
    deployment_id: str,
    unit_id: str,
    base_url: str,
    step: ValidationStep,
) -> None:
    check = registry.create_validation_check(
        attempt_id=attempt_id,
        deployment_id=deployment_id,
        unit_id=unit_id,
        check_type=step.check_type,
        target_ref=step.path,
        expected_json={
            "method": step.method,
            "path": step.path,
            "expected_status": step.expected_status,
            "expected_body_contains": step.expected_body_contains,
        },
        failure_layer=step.failure_layer,
    )
    registry.record_event(deployment_id, "validation_started", f"Validation started: {step.method} {step.path}")
    registry.mark_validation_running(check)
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(f"{base_url}{step.path}")
    except httpx.RequestError as exc:
        message = f"GET {step.path} failed: {exc}"
        registry.mark_validation_failed(
            check,
            observed_json={"error": exc.__class__.__name__, "message": str(exc)},
            failure_layer=step.failure_layer or "gateway",
            message=message,
        )
        registry.record_event(deployment_id, "validation_failed", message, level="error")
        return

    observed_json: dict[str, Any] | None = None
    try:
        parsed_body = response.json()
    except ValueError:
        parsed_body = None
    if isinstance(parsed_body, dict):
        observed_json = parsed_body

    observed = {
        "status": response.status_code,
        "body_json": observed_json,
        "body_text": response.text[:2048] if observed_json is None else None,
    }
    if response.status_code != step.expected_status:
        message = f"GET {step.path} returned {response.status_code}; expected {step.expected_status}"
        registry.mark_validation_failed(
            check,
            observed_json=observed,
            failure_layer=step.failure_layer or "gateway",
            message=message,
        )
        registry.record_event(deployment_id, "validation_failed", message, level="error")
        return
    if not _body_contains(observed_json, step.expected_body_contains):
        message = f"GET {step.path} response body did not include expected validation fields"
        registry.mark_validation_failed(
            check,
            observed_json=observed,
            failure_layer=step.failure_layer or "service_route",
            message=message,
        )
        registry.record_event(deployment_id, "validation_failed", message, level="error")
        return

    message = f"GET {step.path} returned {response.status_code}"
    registry.mark_validation_passed(check, observed_json=observed, message=message)
    registry.record_event(deployment_id, "validation_passed", message)


@router.get("/deployments/{deployment_id}", response_model=DeploymentValidationSummary)
def get_deployment_validation(deployment_id: str, session: Session = Depends(get_db)) -> DeploymentValidationSummary:
    return _summary(RegistryService(session), deployment_id)


@router.post("/deployments/{deployment_id}/run", response_model=DeploymentValidationSummary)
async def run_deployment_validation(deployment_id: str, session: Session = Depends(get_db)) -> DeploymentValidationSummary:
    registry = RegistryService(session)
    deployment = registry.get_deployment(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    base_url = _validation_base_url()
    attempt = registry.create_validation_attempt(
        deployment_id=deployment.deployment_id,
        unit_id=deployment.unit_id,
        validation_base_url=base_url,
        triggered_by="run_deployment_validation",
    )
    if deployment.status != "healthy" or deployment.health_status != "passing":
        registry.mark_validation_attempt_not_ready(
            attempt,
            message="Deployment is not healthy/passing yet; wait_for_deployment should be run before validation.",
        )
        registry.record_event(
            deployment.deployment_id,
            "validation_not_ready",
            "Deployment is not healthy/passing yet; wait_for_deployment should be run before validation.",
            level="warning",
        )
        session.flush()
        return _summary(registry, deployment_id)
    steps = registry.suggested_validation_steps_for_deployment(deployment)
    if not steps:
        return _summary(registry, deployment_id)
    registry.mark_validation_attempt_running(attempt)
    for step in steps:
        await _run_http_step(
            registry,
            attempt_id=attempt.id,
            deployment_id=deployment.deployment_id,
            unit_id=deployment.unit_id,
            base_url=base_url,
            step=step,
        )
    failed_checks = [check for check in registry.get_validation_checks_for_attempt(attempt.id) if check.status == "failed"]
    if failed_checks:
        registry.mark_validation_attempt_failed(attempt, message=failed_checks[-1].message)
    else:
        registry.mark_validation_attempt_passed(attempt, message="Post-deploy validation passed.")
    session.flush()
    return _summary(registry, deployment_id)
