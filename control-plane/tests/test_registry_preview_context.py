from __future__ import annotations


def _add_deployment(
    session,
    *,
    unit_id: str,
    branch: str,
    deployment_id: str,
    commit_sha: str = "abc1234",
    status: str = "healthy",
    health_status: str = "passing",
    deployment_intent: str = "normal_deploy",
    failure_reason: str | None = None,
):
    from app.models.runtime import Deployment

    deployment = Deployment(
        deployment_id=deployment_id,
        unit_id=unit_id,
        branch=branch,
        commit_sha=commit_sha,
        status=status,
        health_status=health_status,
        deployment_intent=deployment_intent,
        failure_reason=failure_reason,
        runtime_ref=(
            {
                "service_name": f"{unit_id}-{deployment_id}",
                "transport": {"scheme": "http", "host": f"{unit_id}-{deployment_id}", "port": 8080},
                "health": {"path": "/health"},
                "proxy": {"base_path": ""},
            }
            if status == "healthy"
            else None
        ),
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


def test_compute_effective_frontend_runtime_state_table() -> None:
    from app.registry.service import compute_effective_frontend_runtime_state
    from app.schemas import FrontendRuntimeDeployment

    baseline = FrontendRuntimeDeployment(
        deployment_id="dep_base",
        branch="main",
        commit_sha="base",
        deployment_status="healthy",
        health_status="passing",
        deployment_intent="revert_to_baseline",
        mode="baseline",
        is_preview=False,
    )
    preview = FrontendRuntimeDeployment(
        deployment_id="dep_preview",
        branch="preview/shell",
        commit_sha="preview",
        deployment_status="healthy",
        health_status="passing",
        deployment_intent="deploy_preview",
        mode="preview",
        is_preview=True,
    )

    cases = [
        (
            "healthy baseline",
            None,
            baseline,
            None,
            "baseline_active",
        ),
        (
            "pending deploy preview over baseline",
            FrontendRuntimeDeployment(
                deployment_id="dep_pending",
                branch="preview/shell",
                commit_sha="pending",
                deployment_status="queued",
                health_status="pending",
                deployment_intent="deploy_preview",
                mode="preview",
                is_preview=True,
            ),
            baseline,
            None,
            "preview_deploying",
        ),
        ("healthy preview", None, preview, preview, "preview_active"),
        (
            "pending revert over preview",
            FrontendRuntimeDeployment(
                deployment_id="dep_revert",
                branch="main",
                commit_sha="base",
                deployment_status="building",
                health_status="pending",
                deployment_intent="revert_to_baseline",
                mode="baseline",
                is_preview=False,
            ),
            preview,
            preview,
            "baseline_reverting",
        ),
        ("back to baseline", None, baseline, baseline, "baseline_active"),
        (
            "failed preview deploy",
            None,
            None,
            FrontendRuntimeDeployment(
                deployment_id="dep_failed",
                branch="preview/shell",
                commit_sha="failed",
                deployment_status="failed",
                health_status="failing",
                deployment_intent="deploy_preview",
                mode="preview",
                is_preview=True,
            ),
            "preview_deploy_failed",
        ),
        (
            "failed revert while preview remains active",
            None,
            preview,
            FrontendRuntimeDeployment(
                deployment_id="dep_revert_failed",
                branch="main",
                commit_sha="base",
                deployment_status="failed",
                health_status="failing",
                deployment_intent="revert_to_baseline",
                mode="baseline",
                is_preview=False,
            ),
            "baseline_revert_failed",
        ),
        ("stale preview terminal with baseline active", None, baseline, preview, "baseline_active"),
        (
            "stale failed deploy with baseline active",
            None,
            baseline,
            FrontendRuntimeDeployment(
                deployment_id="dep_failed",
                branch="preview/shell",
                commit_sha="failed",
                deployment_status="failed",
                health_status="failing",
                deployment_intent="deploy_preview",
                mode="preview",
                is_preview=True,
            ),
            "baseline_active",
        ),
        (
            "missing intent non-baseline",
            FrontendRuntimeDeployment(
                deployment_id="dep_pending",
                branch="feature/foo",
                commit_sha="pending",
                deployment_status="queued",
                health_status="pending",
                deployment_intent="normal_deploy",
                mode="preview",
                is_preview=True,
            ),
            baseline,
            None,
            "preview_deploying",
        ),
        (
            "missing intent baseline",
            FrontendRuntimeDeployment(
                deployment_id="dep_pending",
                branch="main",
                commit_sha="pending",
                deployment_status="queued",
                health_status="pending",
                deployment_intent="normal_deploy",
                mode="baseline",
                is_preview=False,
            ),
            preview,
            None,
            "baseline_reverting",
        ),
    ]

    for _name, pending, active, last_terminal, expected in cases:
        assert (
            compute_effective_frontend_runtime_state(
                pending=pending,
                active=active,
                last_terminal=last_terminal,
                baseline_branch="main",
            )
            == expected
        )


def test_frontend_runtime_status_returns_baseline_active_for_healthy_baseline(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        _add_frontend_shell(session, branch="main", deployment_id="dep_shell_main", commit_sha="main123")
        session.commit()

    response = client.get("/registry/frontend-runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_state"] == "baseline_active"
    assert payload["active"]["mode"] == "baseline"
    assert payload["active"]["is_preview"] is False
    assert payload["pending"] is None


def test_frontend_runtime_status_returns_preview_deploying_when_preview_pending(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        unit = _add_frontend_shell(session, branch="main", deployment_id="dep_shell_main", commit_sha="main123")
        _add_deployment(
            session,
            unit_id=unit.unit_id,
            branch="preview/shell-status",
            deployment_id="dep_shell_pending",
            commit_sha="preview123",
            status="queued",
            health_status="pending",
            deployment_intent="deploy_preview",
        )
        session.commit()

    response = client.get("/registry/frontend-runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_state"] == "preview_deploying"
    assert payload["pending"]["deployment_id"] == "dep_shell_pending"
    assert payload["pending"]["deployment_intent"] == "deploy_preview"


def test_frontend_runtime_status_returns_preview_active_for_healthy_preview(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        _add_frontend_shell(session, branch="preview/shell-status", deployment_id="dep_shell_preview", commit_sha="preview123")
        session.commit()

    response = client.get("/registry/frontend-runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_state"] == "preview_active"
    assert payload["active"]["mode"] == "preview"
    assert payload["active"]["is_preview"] is True


def test_frontend_runtime_status_returns_baseline_reverting_when_revert_pending(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        unit = _add_frontend_shell(session, branch="preview/shell-status", deployment_id="dep_shell_preview", commit_sha="preview123")
        _add_deployment(
            session,
            unit_id=unit.unit_id,
            branch="main",
            deployment_id="dep_shell_revert",
            commit_sha="main123",
            status="building",
            health_status="pending",
            deployment_intent="revert_to_baseline",
        )
        session.commit()

    response = client.get("/registry/frontend-runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_state"] == "baseline_reverting"
    assert payload["pending"]["deployment_id"] == "dep_shell_revert"
    assert payload["active"]["mode"] == "preview"
