from __future__ import annotations

import time
from unittest.mock import ANY, MagicMock

import httpx
import pytest
from starlette.websockets import WebSocketDisconnect

EXPECTED_RUNTIME_PROXY_TIMEOUT = {"connect": 2.0, "read": 8.0, "write": 8.0}
EXPECTED_LONG_RUNTIME_PROXY_TIMEOUT = {"connect": 2.0, "read": 300.0, "write": 300.0}


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
FRONTEND_SHELL_UNIT_ID = "mission-control-frontend-shell"


def _post_and_execute_deployment(client, payload: dict) -> dict:
    from app.config import get_settings
    from app.deployments.worker import DeploymentWorker

    response = client.post("/deployments", json=payload)
    assert response.status_code == 200
    queued = response.json()
    assert DeploymentWorker(get_settings()).run_once() == queued["deployment_id"]
    status = client.get(f"/deployments/{queued['deployment_id']}")
    assert status.status_code == 200
    return status.json()


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
    _post_and_execute_deployment(client, {"unit_id": PROXY_FIXTURE_UNIT_ID, "branch": branch})
    runtime_ref, _ = _active_deployment_payload(PROXY_FIXTURE_UNIT_ID)
    return runtime_ref


def _deploy_frontend_shell_fixture(client) -> dict:
    _post_and_execute_deployment(client, {"unit_id": FRONTEND_SHELL_UNIT_ID, "branch": "main"})
    runtime_ref, _ = _active_deployment_payload(FRONTEND_SHELL_UNIT_ID)
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


def _delete_frontend_shell_unit() -> None:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    with get_session_factory()() as session:
        registry = RegistryService(session)
        unit = registry.get_unit(FRONTEND_SHELL_UNIT_ID)
        assert unit is not None
        session.delete(unit)
        session.commit()


class RecordingAsyncClient:
    def __init__(self, *, follow_redirects: bool, timeout: float | httpx.Timeout):
        self.follow_redirects = follow_redirects
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def build_request(self, method: str, url: str, headers: dict | None = None, content: bytes | None = None):
        request = httpx.Request(method, url, headers=headers, content=content if content is not None else b"")
        request.extensions["recorded_content"] = content
        return request

    async def send(self, request: httpx.Request, *, stream: bool = False):
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "content": request.extensions.get("recorded_content"),
                "follow_redirects": self.follow_redirects,
                "timeout": _serialized_async_timeout(self.timeout),
                "stream": stream,
            }
        )
        return self.response

    async def aclose(self):
        return None


class FakeStreamingUpstreamResponse:
    def __init__(self, *, status_code: int, content: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self._content = content
        self.headers = httpx.Headers(headers)

    async def aiter_bytes(self):
        yield self._content

    async def aclose(self):
        return None


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
            "stream": True,
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
    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})

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
    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
    _set_active_service_runtime_ref("vehicle-config-service", None)

    response = client.get("/registry/services/vehicle-config-service")

    assert response.status_code == 200
    assert response.json()["serviceSlug"] == "vehicle-config-service"


def test_internal_service_proxy_uses_active_deployment_runtime_ref(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
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
            "stream": True,
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

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
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

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
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

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
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
    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
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


def test_internal_service_proxy_returns_503_without_runtime(client) -> None:
    deployment = _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
    _set_active_service_runtime_ref("vehicle-config-service", None)

    response = client.get("/internal/runtime-services/vehicle-config-service/health")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "runtime_service_not_ready",
        "service_slug": "vehicle-config-service",
        "message": "Runtime service is not ready: vehicle-config-service",
        "deployment_id": deployment["deployment_id"],
        "status": "healthy",
    }


def test_internal_service_proxy_returns_503_for_failed_runtime(client) -> None:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    deployment = _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
    deployment_id = deployment["deployment_id"]
    with get_session_factory()() as session:
        registry = RegistryService(session)
        row = registry.get_deployment(deployment_id)
        assert row is not None
        row.status = "failed"
        row.health_status = "failing"
        row.failure_reason = "synthetic failure"
        session.commit()

    response = client.get("/internal/runtime-services/vehicle-config-service/health")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "runtime_service_not_ready",
        "service_slug": "vehicle-config-service",
        "message": "Runtime service is not ready: vehicle-config-service",
        "deployment_id": deployment_id,
        "status": "failed",
    }


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

    def build_request(self, method: str, url: str, **_k):
        return httpx.Request(method, url)

    async def send(self, *_a, **_k):
        raise httpx.ConnectError("connection refused", request=MagicMock())

    async def aclose(self):
        return None


class _ReadTimeoutAsyncClient(_ConnectFailAsyncClient):
    async def send(self, *_a, **_k):
        raise httpx.ReadTimeout("timed out")


def test_internal_service_proxy_returns_502_fast_on_connect_failure(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})

    monkeypatch.setattr(registry_api.httpx, "AsyncClient", _ConnectFailAsyncClient)

    started = time.perf_counter()
    response = client.get("/internal/runtime-services/vehicle-config-service/health")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 502
    assert response.json()["detail"] == "runtime proxy upstream connect failed"
    assert elapsed_ms < 5000


def test_internal_service_proxy_returns_504_fast_on_upstream_read_timeout(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})

    monkeypatch.setattr(registry_api.httpx, "AsyncClient", _ReadTimeoutAsyncClient)

    started = time.perf_counter()
    response = client.get("/internal/runtime-services/vehicle-config-service/health")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert response.status_code == 504
    assert response.json()["detail"] == "runtime proxy upstream timeout"
    assert elapsed_ms < 5000


@pytest.mark.parametrize(
    ("service_slug", "path"),
    [
        ("agent-runtime-service", "chat"),
        ("tool-execution-service", "execute"),
    ],
)
def test_internal_service_proxy_uses_long_timeout_for_allowed_posts(
    client,
    monkeypatch,
    service_slug: str,
    path: str,
) -> None:
    from app.api import registry as registry_api
    from app.schemas import RuntimeHealth, RuntimeProxy, RuntimeRef, RuntimeTransport

    runtime_ref = RuntimeRef(
        service_name=f"{service_slug}-preview",
        transport=RuntimeTransport(scheme="http", host=f"{service_slug}-preview", port=8080),
        health=RuntimeHealth(path="/health"),
        proxy=RuntimeProxy(base_path=""),
    )
    calls: list[dict] = []
    response = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "application/json"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)
    monkeypatch.setattr(registry_api, "_find_service_by_slug", lambda _units, _slug: MagicMock(unit_id=service_slug))
    monkeypatch.setattr(registry_api, "_service_not_ready_payload", lambda _registry, _unit: None)
    monkeypatch.setattr(registry_api, "_get_runtime_ref_for_unit", lambda _registry, _unit: runtime_ref)

    proxied = client.post(f"/internal/runtime-services/{service_slug}/{path}", json={"message": "hello"})

    assert proxied.status_code == 200
    assert proxied.json() == {"ok": True}
    assert calls == [
        {
            "method": "POST",
            "url": f"http://{runtime_ref.transport.host}:{runtime_ref.transport.port}/{path}",
            "headers": ANY,
            "content": b'{"message":"hello"}',
            "follow_redirects": False,
            "timeout": EXPECTED_LONG_RUNTIME_PROXY_TIMEOUT,
            "stream": True,
        }
    ]


def test_internal_service_proxy_uses_default_timeout_for_normal_post(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    _post_and_execute_deployment(client, {"unit_id": "vehicle-config-service", "branch": "main"})
    runtime_ref, _ = _active_deployment_payload("vehicle-config-service")
    calls: list[dict] = []
    response = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "application/json"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.post(
        "/internal/runtime-services/vehicle-config-service/vehicle-configs",
        json={"message": "hello"},
    )

    assert proxied.status_code == 200
    assert proxied.json() == {"ok": True}
    assert calls == [
        {
            "method": "POST",
            "url": (
                f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
                "/vehicle-configs"
            ),
            "headers": ANY,
            "content": b'{"message":"hello"}',
            "follow_redirects": False,
            "timeout": EXPECTED_RUNTIME_PROXY_TIMEOUT,
            "stream": True,
        }
    ]


def test_frontend_shell_proxy_returns_502_when_unit_is_missing(client) -> None:
    _delete_frontend_shell_unit()

    response = client.get("/frontend-shell/")

    assert response.status_code == 502
    assert response.json()["detail"] == "frontend shell unit not found"


def test_frontend_shell_proxy_returns_502_without_active_runtime(client) -> None:
    _set_active_runtime_ref(FRONTEND_SHELL_UNIT_ID, None, active=False)

    response = client.get("/frontend-shell/")

    assert response.status_code == 502
    assert response.json()["detail"] == "frontend shell has no active runtime"


def test_frontend_shell_proxy_uses_active_deployment_runtime_ref(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref = _deploy_frontend_shell_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(200, content=b"shell", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/frontend-shell/apps/telemetry/detail/sat-1?tab=signals")

    assert proxied.status_code == 200
    assert proxied.text == "shell"
    assert calls == [
        {
            "method": "GET",
            "url": (
                f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
                "/apps/telemetry/detail/sat-1?tab=signals"
            ),
            "headers": ANY,
            "content": None,
            "follow_redirects": False,
            "timeout": EXPECTED_RUNTIME_PROXY_TIMEOUT,
            "stream": True,
        }
    ]


def _runtime_ref(*, scheme: str = "http", host: str = "frontend-shell-preview"):
    from app.schemas import RuntimeHealth, RuntimeProxy, RuntimeRef, RuntimeTransport

    return RuntimeRef.model_construct(
        service_name=host,
        transport=RuntimeTransport.model_construct(scheme=scheme, host=host, port=3000),
        health=RuntimeHealth(path="/health"),
        proxy=RuntimeProxy(base_path=""),
    )


def test_build_runtime_websocket_url_maps_http_transport_to_ws() -> None:
    from app.api.registry import _build_runtime_websocket_url

    assert (
        _build_runtime_websocket_url(
            _runtime_ref(scheme="http"),
            path="_next/webpack-hmr",
            query="id=dev-server",
        )
        == "ws://frontend-shell-preview:3000/_next/webpack-hmr?id=dev-server"
    )


def test_build_runtime_websocket_url_maps_https_transport_to_wss() -> None:
    from app.api.registry import _build_runtime_websocket_url

    assert (
        _build_runtime_websocket_url(
            _runtime_ref(scheme="https", host="secure-frontend-shell-preview"),
            path="nested/socket",
        )
        == "wss://secure-frontend-shell-preview:3000/nested/socket"
    )


def test_build_runtime_websocket_url_rejects_invalid_paths() -> None:
    from app.api.registry import _build_runtime_websocket_url
    from app.services.proxy_targets import RuntimeProxyValidationError

    with pytest.raises(RuntimeProxyValidationError, match="proxy path is not allowed"):
        _build_runtime_websocket_url(_runtime_ref(), path="_next/static/../server.js")


def test_frontend_shell_websocket_rejects_production_mode_without_runtime_lookup(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_lookup = MagicMock(side_effect=AssertionError("runtime lookup should not run in production mode"))
    monkeypatch.setattr(registry_api, "_get_frontend_shell_runtime_ref", runtime_lookup)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/frontend-shell/_next/webpack-hmr?id=test"):
            pass

    assert exc_info.value.code == 1008
    runtime_lookup.assert_not_called()


def test_frontend_shell_proxy_strips_decoded_response_framing_headers(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    _deploy_frontend_shell_fixture(client)
    calls: list[dict] = []
    response = FakeStreamingUpstreamResponse(
        status_code=200,
        content=b"<!DOCTYPE html><html><body>control panel</body></html>",
        headers={
            "content-encoding": "gzip",
            "content-length": "9999",
            "transfer-encoding": "chunked",
            "content-type": "text/html; charset=utf-8",
            "x-runtime-id": "shell-preview",
        },
    )
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/frontend-shell/apps/control-panel")

    assert proxied.status_code == 200
    assert proxied.text == "<!DOCTYPE html><html><body>control panel</body></html>"
    assert proxied.headers["content-type"] == "text/html; charset=utf-8"
    assert proxied.headers["x-runtime-id"] == "shell-preview"
    assert "content-encoding" not in proxied.headers
    assert "content-length" not in proxied.headers
    assert "transfer-encoding" not in proxied.headers
    assert calls[0]["stream"] is True


def test_frontend_shell_proxy_preserves_next_static_path_and_query(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref = _deploy_frontend_shell_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(200, content=b"asset", headers={"content-type": "text/javascript"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/frontend-shell/_next/static/chunks/app.js?v=7")

    assert proxied.status_code == 200
    assert proxied.text == "asset"
    assert calls[0]["url"] == (
        f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
        "/_next/static/chunks/app.js?v=7"
    )


def test_frontend_shell_proxy_allows_next_static_css_filename_with_double_dots(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref = _deploy_frontend_shell_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(200, content=b"css", headers={"content-type": "text/css"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/frontend-shell/_next/static/chunks/15_0uk_ih._1..css")

    assert proxied.status_code == 200
    assert proxied.text == "css"
    assert calls[0]["url"] == (
        f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
        "/_next/static/chunks/15_0uk_ih._1..css"
    )


def test_frontend_shell_proxy_strips_only_internal_mount_prefix(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref = _deploy_frontend_shell_fixture(client)
    calls: list[dict] = []
    response = httpx.Response(200, content=b"route", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/frontend-shell/frontend-shell/status")

    assert proxied.status_code == 200
    assert calls[0]["url"] == (
        f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
        "/frontend-shell/status"
    )


def test_frontend_shell_proxy_rejects_malformed_runtime_ref(client) -> None:
    _deploy_frontend_shell_fixture(client)
    _set_active_runtime_ref(FRONTEND_SHELL_UNIT_ID, {"service_name": FRONTEND_SHELL_UNIT_ID})

    proxied = client.get("/frontend-shell/", follow_redirects=False)

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "frontend shell has invalid runtime metadata"
