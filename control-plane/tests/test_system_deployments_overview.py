from __future__ import annotations

from app.services.system_status import ContainerStatus, SystemStatusService


def test_system_deployments_overview_route_returns_aggregate(client, monkeypatch) -> None:
    monkeypatch.setattr(SystemStatusService, "expected_core_services", lambda self: ["postgres"])
    monkeypatch.setattr(
        SystemStatusService,
        "inspect_compose_containers",
        lambda self: (
            {"postgres": ContainerStatus(service="postgres", state="running", status="Up 1 minute", health="healthy")},
            True,
        ),
    )

    response = client.get("/system/deployments/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["core"]["expected_count"] == 1
    assert payload["core"]["existing_count"] == 1
    assert payload["core"]["services"][0]["ui_state"] == "healthy"
    assert payload["runtime"]["expected_count"] == 21
    assert payload["bootstrap"]["status"] == "not_started"
