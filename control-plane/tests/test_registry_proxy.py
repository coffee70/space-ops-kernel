from __future__ import annotations

import time

import httpx
import pytest
from unittest.mock import ANY, MagicMock

EXPECTED_RUNTIME_PROXY_TIMEOUT = {"connect": 2.0, "read": 8.0, "write": 8.0}


def _serialized_async_timeout(timeout: object) -> object:
    """Match httpx.AsyncClient timeouts used by the runtime proxy."""
    if isinstance(timeout, httpx.Timeout):
        return {
            "connect": timeout.connect,
            "read": timeout.read,
            "write": timeout.write,
        }
    return timeout


PROXY_FIXTURE_UNIT_ID = "proxy-backed-test-application"
PROXY_FIXTURE_APP_ID = "proxy-backed-test"
PROXY_FIXTURE_BRANCH = "feature/proxy-backed-test-application"


def _deploy_proxy_fixture(client) -> dict:
    branch = PROXY_FIXTURE_BRANCH
    branch_response = client.post("/code/branches", json={"branch": branch, "from_branch": "main"})
    assert branch_response.status_code == 200
    scaffold_response = client.post(
        "/templates/frontend-embedded-application/scaffold",
        json={
            "branch": branch,
            "unit_id": PROXY_FIXTURE_UNIT_ID,
            "display_name": "Proxy Backed Test",
            "package_owner": "space-ops-apps",
            "discovery": {
                "application_id": PROXY_FIXTURE_APP_ID,
                "description": "Synthetic proxy-backed application fixture.",
            },
        },
    )
    assert scaffold_response.status_code == 200
    commit_response = client.post(
        "/code/commits",
        json={"branch": branch, "message": "Add synthetic proxy application fixture"},
    )
    assert commit_response.status_code == 200
    deployment = client.post("/deployments", json={"unit_id": PROXY_FIXTURE_UNIT_ID, "branch": branch})
    assert deployment.status_code == 200
    runtime_ref, _ = _active_deployment_payload(PROXY_FIXTURE_UNIT_ID)
    return runtime_ref


def _active_deployment_payload(unit_id: str) -> tuple[dict, str]:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    with get_session_factory()() as session:
        registry = RegistryService(session)
        deployment = registry.get_active_deployment_for_unit(unit_id)
        assert deployment is not None
        assert deployment.runtime_ref is not None
        return deployment.runtime_ref, deployment.deployment_id


def _set_active_runtime_ref(unit_id: str, runtime_ref: dict | None, *, active: bool = True) -> None:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    with get_session_factory()() as session:
        registry = RegistryService(session)
        unit = registry.get_unit(unit_id)
        assert unit is not None
        if not active:
            unit.active_deployment_id = None
            session.commit()
            return
        deployment = registry.get_active_deployment_for_unit(unit_id)
        assert deployment is not None
        deployment.runtime_ref = runtime_ref
        session.add(unit)
        session.add(deployment)
        session.commit()


def _set_active_service_runtime_ref(service_slug: str, runtime_ref: dict | None) -> None:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    with get_session_factory()() as session:
        registry = RegistryService(session)
        unit = next(
            candidate
            for candidate in registry.get_units(kind="service")
            if candidate.discovery_metadata_json.get("service_slug") == service_slug
        )
        deployment = registry.get_active_deployment_for_unit(unit.unit_id)
        assert deployment is not None
        deployment.runtime_ref = runtime_ref
        session.add(deployment)
        session.commit()


def _set_active_application_runtime_ref(application_id: str, runtime_ref: dict | None) -> None:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    with get_session_factory()() as session:
        registry = RegistryService(session)
        deployment = registry.get_active_application_deployment(application_id)
        assert deployment is not None
        deployment.runtime_ref = runtime_ref
        session.add(deployment)
        session.commit()


class RecordingAsyncClient:
    def __init__(self, *, follow_redirects: bool, timeout: float | httpx.Timeout):
        self.follow_redirects = follow_redirects
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, method: str, url: str, headers: dict | None = None, content: bytes | None = None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "content": content,
                "follow_redirects": self.follow_redirects,
                "timeout": _serialized_async_timeout(self.timeout),
            }
        )
        return self.response


def test_application_proxy_uses_active_deployment_runtime_ref(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref = _deploy_proxy_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "application/json"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/runtime-applications/proxy-backed-test/events?limit=10")

    assert proxied.status_code == 200
    assert proxied.json() == {"ok": True}
    assert calls == [
        {
            "method": "GET",
            "url": (
                f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
                "/runtime-applications/proxy-backed-test/events?limit=10"
            ),
            "headers": ANY,
            "content": None,
            "follow_redirects": False,
            "timeout": EXPECTED_RUNTIME_PROXY_TIMEOUT,
        }
    ]


def test_application_proxy_prefixes_proxy_base_path(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref = _deploy_proxy_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/runtime-applications/proxy-backed-test/assets/app.js?v=7")

    assert proxied.status_code == 200
    assert proxied.text == "ok"
    assert calls[0]["url"] == (
        f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
        "/runtime-applications/proxy-backed-test/assets/app.js?v=7"
    )
    assert calls[0]["follow_redirects"] is False


@pytest.mark.parametrize(
    "path",
    [
        "../secrets",
        "%2e%2e/secrets",
        "%252e%252e/secrets",
        "a//b",
        "a%2fb",
        r"a\b",
        "a%5cb",
        "http:%2f%2fevil.example/x",
        "https:%2f%2fevil.example/x",
    ],
)
def test_application_proxy_rejects_unsafe_path_fragments(client, monkeypatch, path: str) -> None:
    from app.api import registry as registry_api

    _deploy_proxy_fixture(client)

    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get(f"/runtime-applications/proxy-backed-test/{path}")

    assert proxied.status_code in {400, 404}
    assert calls == []


def test_proxy_rejects_malformed_runtime_ref(client) -> None:
    _deploy_proxy_fixture(client)
    _set_active_application_runtime_ref(PROXY_FIXTURE_APP_ID, {"service_name": PROXY_FIXTURE_UNIT_ID})

    proxied = client.get("/runtime-applications/proxy-backed-test", follow_redirects=False)

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "application has invalid runtime metadata"


def test_proxy_does_not_follow_redirects(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    _deploy_proxy_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(
        307,
        content=b"",
        headers={"location": "http://should-not-be-followed/internal", "content-type": "text/plain"},
    )
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/runtime-applications/proxy-backed-test", follow_redirects=False)

    assert proxied.status_code == 307
    assert proxied.headers["location"] == "http://should-not-be-followed/internal"
    assert calls[0]["follow_redirects"] is False


def test_proxy_rejects_host_service_name_mismatch(client) -> None:
    runtime_ref = _deploy_proxy_fixture(client)
    runtime_ref["transport"]["host"] = "unexpected-runtime"
    _set_active_application_runtime_ref(PROXY_FIXTURE_APP_ID, runtime_ref)

    proxied = client.get("/runtime-applications/proxy-backed-test")

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "runtime proxy host must match service_name"


def test_registry_service_lookup_returns_safe_service_metadata(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200

    response = client.get("/registry/services/vehicle-config-service")

    assert response.status_code == 200
    payload = response.json()
    assert payload["serviceSlug"] == "vehicle-config-service"
    assert payload["unitId"] == "vehicle-config-service"
    assert payload["deploymentStatus"] == "healthy"
    assert payload["healthStatus"] == "passing"
    assert "runtime_endpoint" not in payload
    assert "runtimeEndpoint" not in payload
    assert "host" not in payload
    assert "port" not in payload
    assert "active_deployment_id" not in payload
    assert "source_path" not in payload
    assert "discovery_metadata_json" not in payload


def test_registry_service_lookup_returns_404_for_unknown_slug(client) -> None:
    response = client.get("/registry/services/unknown-service")

    assert response.status_code == 404
    assert response.json()["detail"] == "service not found"


def test_registry_service_lookup_does_not_require_active_runtime(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    _set_active_service_runtime_ref("vehicle-config-service", None)

    response = client.get("/registry/services/vehicle-config-service")

    assert response.status_code == 200
    assert response.json()["serviceSlug"] == "vehicle-config-service"


def test_internal_service_proxy_uses_active_deployment_runtime_ref(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    runtime_ref, _ = _active_deployment_payload("vehicle-config-service")
    calls: list[dict] = []
    response = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "application/json"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/internal/runtime-services/vehicle-config-service/vehicle-configs?limit=10")

    assert proxied.status_code == 200
    assert proxied.json() == {"ok": True}
    assert calls == [
        {
            "method": "GET",
            "url": (
                f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
                "/vehicle-configs?limit=10"
            ),
            "headers": ANY,
            "content": None,
            "follow_redirects": False,
            "timeout": EXPECTED_RUNTIME_PROXY_TIMEOUT,
        }
    ]


@pytest.mark.parametrize(
    "path",
    [
        "../secrets",
        "%2e%2e/secrets",
        "%252e%252e/secrets",
        "a//b",
        "a%2fb",
        r"a\b",
        "a%5cb",
        "http:%2f%2fevil.example/x",
        "https:%2f%2fevil.example/x",
    ],
)
def test_internal_service_proxy_rejects_unsafe_path_fragments(client, monkeypatch, path: str) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get(f"/internal/runtime-services/vehicle-config-service/{path}")

    assert proxied.status_code in {400, 404}
    assert calls == []


def test_internal_service_proxy_strips_sensitive_headers(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get(
        "/internal/runtime-services/vehicle-config-service/vehicle-configs",
        headers={
            "authorization": "Bearer secret",
            "cookie": "session=secret",
            "x-api-key": "secret",
            "x-forwarded-user": "operator",
            "connection": "close",
        },
    )

    assert proxied.status_code == 200
    forwarded_headers = {key.lower(): value for key, value in calls[0]["headers"].items()}
    assert "authorization" not in forwarded_headers
    assert "cookie" not in forwarded_headers
    assert "x-api-key" not in forwarded_headers
    assert "x-forwarded-user" not in forwarded_headers
    assert "connection" not in forwarded_headers


def test_internal_service_proxy_does_not_follow_redirects(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    calls: list[dict] = []
    response = httpx.Response(
        307,
        content=b"",
        headers={"location": "http://should-not-be-followed/internal", "content-type": "text/plain"},
    )
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/internal/runtime-services/vehicle-config-service", follow_redirects=False)

    assert proxied.status_code == 307
    assert proxied.headers["location"] == "http://should-not-be-followed/internal"
    assert calls[0]["follow_redirects"] is False


def test_internal_service_proxy_rejects_host_service_name_mismatch(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    runtime_ref, _ = _active_deployment_payload("vehicle-config-service")
    runtime_ref["transport"]["host"] = "unexpected-runtime"
    _set_active_service_runtime_ref("vehicle-config-service", runtime_ref)

    proxied = client.get("/internal/runtime-services/vehicle-config-service")

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "runtime proxy host must match service_name"


def test_internal_service_proxy_returns_404_for_unknown_service_slug(client) -> None:
    response = client.get("/internal/runtime-services/unknown-service/health")

    assert response.status_code == 404
    assert response.json()["detail"] == "service not found"


def test_internal_service_proxy_returns_502_without_runtime(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    _set_active_service_runtime_ref("vehicle-config-service", None)

    response = client.get("/internal/runtime-services/vehicle-config-service/health")

    assert response.status_code == 502
    assert response.json()["detail"] == "service has no active runtime"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/".join(("", "proxy", "services", "ops-events", "health"))),
        ("GET", "/".join(("", "proxy", "services", "ops-events-service", "health"))),
        ("GET", "/".join(("", "proxy", "units", "ops-events-service", "health"))),
        ("POST", "/".join(("", "proxy", "units", "ops-events-service", "health"))),
    ],
)
def test_legacy_proxy_routes_return_404(client, method: str, path: str) -> None:
    response = client.request(method, path, follow_redirects=False)

    assert response.status_code == 404


class _ConnectFailAsyncClient:
    """AsyncClient shim that raises on first upstream request."""

    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def request(self, *_a, **_k):
        raise httpx.ConnectError("connection refused", request=MagicMock())


class _ReadTimeoutAsyncClient(_ConnectFailAsyncClient):
    async def request(self, *_a, **_k):
        raise httpx.ReadTimeout("timed out")


def test_internal_service_proxy_returns_502_fast_on_connect_failure(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200

    monkeypatch.setattr(registry_api.httpx, "AsyncClient", _ConnectFailAsyncClient)

    started = time.perf_counter()
    response = client.get("/internal/runtime-services/vehicle-config-service/health")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 502
    assert response.json()["detail"] == "runtime proxy upstream connect failed"
    assert elapsed_ms < 5000


def test_internal_service_proxy_returns_504_fast_on_upstream_read_timeout(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200

    monkeypatch.setattr(registry_api.httpx, "AsyncClient", _ReadTimeoutAsyncClient)

    started = time.perf_counter()
    response = client.get("/internal/runtime-services/vehicle-config-service/health")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 504
    assert response.json()["detail"] == "runtime proxy upstream timeout"
    assert elapsed_ms < 5000
