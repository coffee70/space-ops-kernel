from __future__ import annotations

import httpx
import pytest
from unittest.mock import ANY


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
    def __init__(self, *, follow_redirects: bool, timeout: float):
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
                "timeout": self.timeout,
            }
        )
        return self.response


def test_application_proxy_uses_active_deployment_runtime_ref(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
    assert deployment.status_code == 200
    runtime_ref, _ = _active_deployment_payload("embedded-demo-application")
    calls: list[dict] = []
    response = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "application/json"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/runtime-applications/embedded-demo/events?limit=10")

    assert proxied.status_code == 200
    assert proxied.json() == {"ok": True}
    assert calls == [
        {
            "method": "GET",
            "url": (
                f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
                "/runtime-applications/embedded-demo/events?limit=10"
            ),
            "headers": ANY,
            "content": None,
            "follow_redirects": False,
            "timeout": 30.0,
        }
    ]


def test_application_proxy_prefixes_proxy_base_path(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
    assert deployment.status_code == 200
    runtime_ref, _ = _active_deployment_payload("embedded-demo-application")
    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/runtime-applications/embedded-demo/assets/app.js?v=7")

    assert proxied.status_code == 200
    assert proxied.text == "ok"
    assert calls[0]["url"] == (
        f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
        "/runtime-applications/embedded-demo/assets/app.js?v=7"
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

    deployment = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
    assert deployment.status_code == 200

    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get(f"/runtime-applications/embedded-demo/{path}")

    assert proxied.status_code in {400, 404}
    assert calls == []


def test_proxy_rejects_malformed_runtime_ref(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
    assert deployment.status_code == 200
    _set_active_application_runtime_ref("embedded-demo", {"service_name": "embedded-demo-application"})

    proxied = client.get("/runtime-applications/embedded-demo", follow_redirects=False)

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "application has invalid runtime metadata"


def test_proxy_does_not_follow_redirects(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    deployment = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
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

    proxied = client.get("/runtime-applications/embedded-demo", follow_redirects=False)

    assert proxied.status_code == 307
    assert proxied.headers["location"] == "http://should-not-be-followed/internal"
    assert calls[0]["follow_redirects"] is False


def test_proxy_rejects_host_service_name_mismatch(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
    assert deployment.status_code == 200
    runtime_ref, _ = _active_deployment_payload("embedded-demo-application")
    runtime_ref["transport"]["host"] = "unexpected-runtime"
    _set_active_application_runtime_ref("embedded-demo", runtime_ref)

    proxied = client.get("/runtime-applications/embedded-demo")

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "runtime proxy host must match service_name"


def test_registry_service_lookup_returns_active_runtime_endpoint(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200

    response = client.get("/registry/services/vehicle-config-service")

    assert response.status_code == 200
    payload = response.json()
    assert payload["unit_id"] == "vehicle-config-service"
    assert payload["runtime_endpoint"] == {
        "service_name": payload["runtime_endpoint"]["host"],
        "host": payload["runtime_endpoint"]["host"],
        "port": 8080,
        "proxy_base_path": "",
        "health_path": "/health",
    }


def test_registry_service_lookup_returns_404_for_unknown_slug(client) -> None:
    response = client.get("/registry/services/unknown-service")

    assert response.status_code == 404
    assert response.json()["detail"] == "service not found"


def test_registry_service_lookup_returns_502_without_runtime(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "vehicle-config-service", "branch": "main"})
    assert deployment.status_code == 200
    _set_active_service_runtime_ref("vehicle-config-service", None)

    response = client.get("/registry/services/vehicle-config-service")

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
