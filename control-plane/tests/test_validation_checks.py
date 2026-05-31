from __future__ import annotations


def _add_healthy_service_deployment(session, *, discovery: dict | None = None):
    from app.models.runtime import Deployment, ManagedUnit

    unit = ManagedUnit(
        unit_id="validation-fixture-service",
        display_name="Validation Fixture Service",
        package_owner="space-ops-platform",
        runtime_kind="service",
        runtime_template="python-service",
        source_path="project/space-ops-platform/backend/services/validation-fixture-service",
        active_deployment_id="dep_validation_fixture",
        deployment_status="healthy",
        health_status="passing",
        discovery_metadata_json=discovery
        or {
            "service_slug": "validation-fixture-service",
            "health_endpoint": "/health",
            "primary_routes": [{"path": "/metadata", "method": "GET", "expected_status": 200}],
        },
    )
    session.add(unit)
    session.flush()
    deployment = Deployment(
        deployment_id="dep_validation_fixture",
        unit_id=unit.unit_id,
        branch="feature/validation",
        commit_sha="abc1234",
        status="healthy",
        health_status="passing",
        runtime_ref={
            "service_name": "validation-fixture-service-dep",
            "transport": {"scheme": "http", "host": "validation-fixture-service-dep", "port": 8080},
            "health": {"path": "/health"},
            "proxy": {"base_path": ""},
        },
    )
    session.add(deployment)
    session.flush()
    return deployment


def _add_healthy_frontend_deployment(session, *, runtime_kind: str, unit_id: str, deployment_id: str):
    from app.models.runtime import Application, ApplicationDeployment, Deployment, ManagedUnit

    unit = ManagedUnit(
        unit_id=unit_id,
        display_name=unit_id,
        package_owner="space-ops-apps",
        runtime_kind=runtime_kind,
        runtime_template="frontend-shell" if runtime_kind == "frontend_shell" else "frontend-native-application",
        source_path="project/space-ops-apps/mission-control-ui",
        active_deployment_id=deployment_id,
        deployment_status="healthy",
        health_status="passing",
        discovery_metadata_json={},
    )
    session.add(unit)
    deployment = Deployment(
        deployment_id=deployment_id,
        unit_id=unit.unit_id,
        branch="feature/frontend-validation",
        commit_sha="frontend123",
        status="healthy",
        health_status="passing",
        runtime_ref={
            "service_name": f"{unit_id}-dep",
            "transport": {"scheme": "http", "host": f"{unit_id}-dep", "port": 8080},
            "health": {"path": "/health"},
            "proxy": {"base_path": ""},
        },
    )
    session.add(deployment)
    if runtime_kind == "frontend_application":
        session.add(
            Application(
                application_id="validation-app",
                title="Validation App",
                description="Validation app.",
                icon_key="test",
                icon_color="#ffffff",
                icon_background="rgba(255,255,255,0.16)",
                application_type="native",
                route_path="/apps/validation-app",
                loader_key="validation-app",
                embedded_url=None,
                proxy_base_path=None,
                version="0.1.0",
                enabled=True,
                sort_order=100,
                owner="tests",
                health_status="passing",
                deployment_status="healthy",
            )
        )
        session.add(
            ApplicationDeployment(
                deployment_id=deployment_id,
                application_id="validation-app",
                commit_sha="frontend123",
                status="healthy",
                health_status="passing",
            )
        )
    session.flush()
    return deployment


def test_deployment_status_includes_validation_gate_and_next_steps(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        _add_healthy_service_deployment(session)
        session.commit()

    response = client.get("/deployments/dep_validation_fixture")

    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_status"] == "not_run"
    assert payload["success_claim_allowed"] is False
    assert payload["next_validation_steps"] == [
        {
            "check_type": "service_health_gateway",
            "method": "GET",
            "path": "/internal/runtime-services/validation-fixture-service/health",
            "expected_status": 200,
            "expected_body_contains": None,
            "failure_layer": "gateway",
        },
        {
            "check_type": "service_primary_route_1",
            "method": "GET",
            "path": "/internal/runtime-services/validation-fixture-service/metadata",
            "expected_status": 200,
            "expected_body_contains": None,
            "failure_layer": "service_route",
        },
    ]


def test_frontend_application_validation_uses_operator_and_proxy_routes(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        _add_healthy_frontend_deployment(
            session,
            runtime_kind="frontend_application",
            unit_id="validation-frontend-app",
            deployment_id="dep_validation_frontend_app",
        )
        session.commit()

    response = client.get("/deployments/dep_validation_frontend_app")

    assert response.status_code == 200
    assert response.json()["next_validation_steps"] == [
        {
            "check_type": "frontend_application_operator_route",
            "method": "GET",
            "path": "/apps/validation-app",
            "expected_status": 200,
            "expected_body_contains": None,
            "failure_layer": "frontend_route",
        },
        {
            "check_type": "frontend_application_runtime_proxy",
            "method": "GET",
            "path": "/runtime-applications/validation-app",
            "expected_status": 200,
            "expected_body_contains": None,
            "failure_layer": "frontend_route",
        },
    ]


def test_frontend_shell_validation_uses_edge_root_and_direct_proxy(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        _add_healthy_frontend_deployment(
            session,
            runtime_kind="frontend_shell",
            unit_id="validation-frontend-shell",
            deployment_id="dep_validation_frontend_shell",
        )
        session.commit()

    response = client.get("/deployments/dep_validation_frontend_shell")

    assert response.status_code == 200
    assert response.json()["next_validation_steps"] == [
        {
            "check_type": "frontend_shell_operator_root",
            "method": "GET",
            "path": "/",
            "expected_status": 200,
            "expected_body_contains": None,
            "failure_layer": "frontend_route",
        },
        {
            "check_type": "frontend_shell_direct_proxy",
            "method": "GET",
            "path": "/frontend-shell",
            "expected_status": 200,
            "expected_body_contains": None,
            "failure_layer": "frontend_route",
        },
    ]


def test_run_deployment_validation_persists_passing_checks(client, monkeypatch) -> None:
    from app.api import validation
    from app.db import get_session_factory
    from app.models.runtime import ValidationAttempt, ValidationCheck

    class FakeResponse:
        status_code = 200
        text = '{"status":"ok"}'

        def json(self):
            return {"status": "ok"}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            assert url.startswith("http://telemetry-platform-edge-proxy:8080")
            assert url.endswith(("/health", "/metadata"))
            return FakeResponse()

    with get_session_factory()() as session:
        _add_healthy_service_deployment(session)
        session.commit()

    monkeypatch.setattr(validation.httpx, "AsyncClient", FakeAsyncClient)
    response = client.post("/validation/deployments/dep_validation_fixture/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_status"] == "passed"
    assert payload["latest_attempt"]["status"] == "passed"
    assert payload["latest_attempt"]["validation_base_url"] == "http://telemetry-platform-edge-proxy:8080"
    assert len(payload["attempts"]) == 1
    assert [check["status"] for check in payload["checks"]] == ["passed", "passed"]
    assert all(check["attempt_id"] == payload["latest_attempt"]["id"] for check in payload["checks"])

    with get_session_factory()() as session:
        assert session.query(ValidationAttempt).count() == 1
        assert session.query(ValidationCheck).filter(ValidationCheck.attempt_id == payload["latest_attempt"]["id"]).count() == 2

    deployment = client.get("/deployments/dep_validation_fixture").json()
    assert deployment["validation_status"] == "passed"
    assert deployment["success_claim_allowed"] is True


def test_run_deployment_validation_records_route_failure(client, monkeypatch) -> None:
    from app.api import validation
    from app.db import get_session_factory

    class FakeResponse:
        text = "not found"

        def __init__(self, status_code: int):
            self.status_code = status_code

        def json(self):
            raise ValueError("not json")

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            return FakeResponse(404 if url.endswith("/metadata") else 200)

    with get_session_factory()() as session:
        _add_healthy_service_deployment(session)
        session.commit()

    monkeypatch.setattr(validation.httpx, "AsyncClient", FakeAsyncClient)
    response = client.post("/validation/deployments/dep_validation_fixture/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_status"] == "failed"
    failed = [check for check in payload["checks"] if check["status"] == "failed"]
    assert failed[0]["failure_layer"] == "service_route"
    assert "returned 404" in failed[0]["message"]


def test_run_deployment_validation_appends_attempts_and_summarizes_latest(client, monkeypatch) -> None:
    from app.api import validation
    from app.db import get_session_factory
    from app.models.runtime import ValidationAttempt, ValidationCheck

    class FakeResponse:
        text = '{"status":"ok"}'

        def __init__(self, status_code: int):
            self.status_code = status_code

        def json(self):
            return {"status": "ok"}

    call_count = 0

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return FakeResponse(404 if url.endswith("/metadata") else 200)
            return FakeResponse(200)

    with get_session_factory()() as session:
        _add_healthy_service_deployment(session)
        session.commit()

    monkeypatch.setattr(validation.httpx, "AsyncClient", FakeAsyncClient)
    first = client.post("/validation/deployments/dep_validation_fixture/run")
    assert first.status_code == 200
    assert first.json()["validation_status"] == "failed"

    second = client.post("/validation/deployments/dep_validation_fixture/run")
    assert second.status_code == 200
    payload = second.json()
    assert payload["validation_status"] == "passed"
    assert len(payload["attempts"]) == 2
    assert payload["attempts"][0]["status"] == "failed"
    assert payload["attempts"][1]["status"] == "passed"
    assert len(payload["checks"]) == 2
    assert all(check["status"] == "passed" for check in payload["checks"])
    assert all(check["attempt_id"] == payload["latest_attempt"]["id"] for check in payload["checks"])

    with get_session_factory()() as session:
        attempts = session.query(ValidationAttempt).order_by(ValidationAttempt.created_at.asc()).all()
        assert [attempt.status for attempt in attempts] == ["failed", "passed"]
        assert session.query(ValidationCheck).count() == 4
        assert session.query(ValidationCheck).filter(ValidationCheck.attempt_id == attempts[0].id).count() == 2
        assert session.query(ValidationCheck).filter(ValidationCheck.attempt_id == attempts[1].id).count() == 2


def test_run_deployment_validation_records_not_ready_attempt(client) -> None:
    from app.db import get_session_factory

    with get_session_factory()() as session:
        deployment = _add_healthy_service_deployment(session)
        deployment.status = "building"
        deployment.health_status = "pending"
        session.commit()

    response = client.post("/validation/deployments/dep_validation_fixture/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["validation_status"] == "not_ready"
    assert payload["latest_attempt"]["status"] == "not_ready"
    assert "not healthy/passing" in payload["latest_attempt"]["message"]
    assert payload["checks"] == []
