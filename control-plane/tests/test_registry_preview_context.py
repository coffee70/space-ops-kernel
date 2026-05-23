from __future__ import annotations


def _add_deployment(session, *, unit_id: str, branch: str, deployment_id: str, commit_sha: str = "abc1234"):
    from app.models.runtime import Deployment

    deployment = Deployment(
        deployment_id=deployment_id,
        unit_id=unit_id,
        branch=branch,
        commit_sha=commit_sha,
        status="healthy",
        health_status="passing",
        runtime_ref={
            "service_name": f"{unit_id}-{deployment_id}",
            "transport": {"scheme": "http", "host": f"{unit_id}-{deployment_id}", "port": 8080},
            "health": {"path": "/health"},
            "proxy": {"base_path": ""},
        },
    )
    session.add(deployment)
    session.flush()
    return deployment


def _add_frontend_shell(
    session,
    *,
    branch: str,
    deployment_id: str = "dep_shell",
    commit_sha: str = "abc1234",
    discovery: dict | None = None,
):
    from app.models.runtime import ManagedUnit

    unit = session.get(ManagedUnit, "mission-control-frontend-shell")
    if unit is None:
        unit = ManagedUnit(
            unit_id="mission-control-frontend-shell",
            display_name="Mission Control Frontend Shell",
            package_owner="space-ops-apps",
            runtime_kind="frontend_shell",
            runtime_template="frontend-shell",
            source_path="project/space-ops-apps/mission-control-ui",
        )
        session.add(unit)
        session.flush()
    unit.deployment_status = "healthy"
    unit.health_status = "passing"
    unit.discovery_metadata_json = discovery or {}
    deployment = _add_deployment(
        session,
        unit_id="mission-control-frontend-shell",
        branch=branch,
        deployment_id=deployment_id,
        commit_sha=commit_sha,
    )
    unit.active_deployment_id = deployment.deployment_id
    session.flush()
    return unit


def test_frontend_runtime_preview_context_returns_baseline_when_no_shell(client) -> None:
    response = client.get("/registry/frontend-runtime/preview-context")

    assert response.status_code == 200
    assert response.json() == {
        "is_preview": False,
        "frontend_unit_id": "mission-control-frontend-shell",
        "active_deployment_id": None,
        "runtime_service_name": None,
        "branch": None,
        "commit_sha": None,
        "deployment_status": "pending",
        "health_status": "unknown",
        "baseline_branch": "main",
        "baseline_commit_sha": None,
        "preview_deployment_id": None,
        "target_application_id": None,
    }


def test_frontend_runtime_preview_context_does_not_show_for_main_shell(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        _add_frontend_shell(session, branch="main", deployment_id="dep_shell_main", commit_sha="main123")
        session.commit()

    response = client.get("/registry/frontend-runtime/preview-context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_preview"] is False
    assert payload["frontend_unit_id"] == "mission-control-frontend-shell"
    assert payload["branch"] == "main"
    assert payload["commit_sha"] == "main123"
    assert payload["preview_deployment_id"] is None


def test_frontend_runtime_preview_context_shows_non_main_shell_preview(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import ManagedBranch

    with get_session_factory()() as session:
        _add_frontend_shell(
            session,
            branch="preview/shell-banner",
            deployment_id="dep_shell_preview",
            commit_sha="preview123456",
            discovery={"target_application_id": "telemetry"},
        )
        session.add(
            ManagedBranch(
                branch_name="preview/shell-banner",
                repository_root="/tmp/repo.git",
                worktree_path="/tmp/worktrees/preview-shell-banner",
                base_branch="main",
                base_commit_sha="baseline123",
                created_commit_sha="preview123456",
                associated_unit_id="mission-control-frontend-shell",
                associated_deployment_id="dep_shell_preview",
            )
        )
        session.commit()

    response = client.get("/registry/frontend-runtime/preview-context")

    assert response.status_code == 200
    payload = response.json()
    assert payload["is_preview"] is True
    assert payload["frontend_unit_id"] == "mission-control-frontend-shell"
    assert payload["active_deployment_id"] == "dep_shell_preview"
    assert payload["runtime_service_name"] == "mission-control-frontend-shell-dep_shell_preview"
    assert payload["preview_deployment_id"] == "dep_shell_preview"
    assert payload["branch"] == "preview/shell-banner"
    assert payload["commit_sha"] == "preview123456"
    assert payload["baseline_branch"] == "main"
    assert payload["baseline_commit_sha"] == "baseline123"
    assert payload["target_application_id"] == "telemetry"


def test_frontend_runtime_preview_context_ignores_backend_preview_target_application(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Deployment, ManagedUnit

    with get_session_factory()() as session:
        session.add(
            ManagedUnit(
                unit_id="backend-preview-service",
                display_name="Backend Preview Service",
                package_owner="space-ops-platform",
                runtime_kind="service",
                runtime_template="python-service",
                source_path="project/space-ops-platform/backend/services/backend-preview-service",
                active_deployment_id="dep_backend_preview",
                deployment_status="healthy",
                health_status="passing",
                discovery_metadata_json={"target_application_id": "telemetry"},
            )
        )
        session.flush()
        session.add(
            Deployment(
                deployment_id="dep_backend_preview",
                unit_id="backend-preview-service",
                branch="preview/backend-with-target-app",
                commit_sha="backend123",
                status="healthy",
                health_status="passing",
            )
        )
        session.get(ManagedUnit, "backend-preview-service").active_deployment_id = "dep_backend_preview"
        session.commit()

    response = client.get("/registry/frontend-runtime/preview-context")

    assert response.status_code == 200
    assert response.json()["is_preview"] is False
