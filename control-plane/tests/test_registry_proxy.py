from __future__ import annotations

import httpx
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


def test_service_proxy_uses_active_deployment_runtime_ref(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref, _ = _active_deployment_payload("ops-events-service")
    calls: list[dict] = []
    response = httpx.Response(200, content=b'{"ok":true}', headers={"content-type": "application/json"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/proxy/services/ops-events-service/events?limit=10")

    assert proxied.status_code == 200
    assert proxied.json() == {"ok": True}
    assert calls == [
        {
            "method": "GET",
            "url": (
                f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
                "/events?limit=10"
            ),
            "headers": ANY,
            "content": None,
            "follow_redirects": False,
            "timeout": 30.0,
        }
    ]


def test_application_proxy_prefixes_proxy_base_path(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    runtime_ref, _ = _active_deployment_payload("battery-efficiency-application")
    calls: list[dict] = []
    response = httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/runtime-applications/battery-efficiency/assets/app.js?v=7")

    assert proxied.status_code == 200
    assert proxied.text == "ok"
    assert calls[0]["url"] == (
        f"http://{runtime_ref['transport']['host']}:{runtime_ref['transport']['port']}"
        "/runtime-applications/battery-efficiency/assets/app.js?v=7"
    )
    assert calls[0]["follow_redirects"] is False


def test_unit_proxy_returns_404_without_active_deployment(client) -> None:
    _set_active_runtime_ref("ops-events-service", None, active=False)

    proxied = client.get("/proxy/units/ops-events-service")

    assert proxied.status_code == 404
    assert proxied.json()["detail"] == "unit not found"


def test_proxy_rejects_malformed_runtime_ref(client) -> None:
    _set_active_runtime_ref("ops-events-service", {"service_name": "ops-events-service"})

    proxied = client.get("/proxy/services/ops-events-service", follow_redirects=False)

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "unit has invalid runtime metadata"


def test_proxy_does_not_follow_redirects(client, monkeypatch) -> None:
    from app.api import registry as registry_api

    calls: list[dict] = []
    response = httpx.Response(
        307,
        content=b"",
        headers={"location": "http://should-not-be-followed/internal", "content-type": "text/plain"},
    )
    RecordingAsyncClient.calls = calls
    RecordingAsyncClient.response = response
    monkeypatch.setattr(registry_api.httpx, "AsyncClient", RecordingAsyncClient)

    proxied = client.get("/proxy/services/ops-events-service", follow_redirects=False)

    assert proxied.status_code == 307
    assert proxied.headers["location"] == "http://should-not-be-followed/internal"
    assert calls[0]["follow_redirects"] is False


def test_proxy_rejects_host_service_name_mismatch(client) -> None:
    runtime_ref, _ = _active_deployment_payload("ops-events-service")
    runtime_ref["transport"]["host"] = "unexpected-runtime"
    _set_active_runtime_ref("ops-events-service", runtime_ref)

    proxied = client.get("/proxy/services/ops-events-service")

    assert proxied.status_code == 502
    assert proxied.json()["detail"] == "runtime proxy host must match service_name"
