from __future__ import annotations


def test_registry_application_enable_route_is_not_exposed(client) -> None:
    response = client.post("/registry/applications/overview/enable")

    assert response.status_code == 404


def test_registry_application_disable_route_is_not_exposed(client) -> None:
    response = client.post("/registry/applications/overview/disable")

    assert response.status_code == 404


def test_get_registry_application_returns_seeded_overview(client) -> None:
    response = client.get("/registry/applications/overview")

    assert response.status_code == 200
    assert response.json() == {
        "applicationId": "overview",
        "title": "Overview",
        "description": "Mission overview dashboard.",
        "iconKey": "layout-dashboard",
        "iconColor": "#38bdf8",
        "iconBackground": "rgba(56, 189, 248, 0.16)",
        "applicationType": "native",
        "routePath": "/apps/overview",
        "loaderKey": "overview",
        "embeddedUrl": None,
        "proxyBasePath": None,
        "version": "0.1.0",
        "enabled": True,
        "iframeSandbox": None,
        "iframeAllow": None,
        "sortOrder": 10,
        "owner": "space-ops-apps",
        "capabilities": ["telemetry-overview"],
        "healthStatus": "unknown",
        "deploymentStatus": "seeded",
    }


def test_get_registry_applications_returns_seeded_catalog_in_order(client) -> None:
    response = client.get("/registry/applications")

    assert response.status_code == 200
    payload = response.json()
    assert [item["applicationId"] for item in payload] == [
        "overview",
        "telemetry",
        "planning",
        "sources",
        "workspace",
        "battery-efficiency",
    ]

    workspace = next(item for item in payload if item["applicationId"] == "workspace")
    assert workspace["applicationType"] == "embedded"
    assert workspace["proxyBasePath"] == "/workspace"
    assert workspace["embeddedUrl"] == "/workspace"
    assert workspace["enabled"] is True
    assert workspace["sortOrder"] == 50
