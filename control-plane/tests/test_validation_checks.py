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


def test_run_deployment_validation_persists_passing_checks(client, monkeypatch) -> None:
    from app.api import validation
    from app.db import get_session_factory

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
    assert [check["status"] for check in payload["checks"]] == ["passed", "passed"]

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
