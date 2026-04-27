from __future__ import annotations

from app.db import get_session_factory
from app.models.runtime import ApplicationAuditEvent


def test_registry_application_enable_route_is_exposed(client) -> None:
    response = client.post("/registry/applications/overview/enable")

    assert response.status_code == 200


def test_registry_application_disable_route_is_exposed(client) -> None:
    response = client.post("/registry/applications/overview/disable")

    assert response.status_code == 200


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
        "embedded-demo",
    ]

    workspace = next(item for item in payload if item["applicationId"] == "workspace")
    assert workspace["applicationType"] == "embedded"
    assert workspace["proxyBasePath"] is None
    assert workspace["embeddedUrl"] == "/_embedded/workspace"
    assert workspace["enabled"] is True
    assert workspace["sortOrder"] == 50


def test_enable_endpoint_sets_enabled_true_and_returns_updated_payload(client) -> None:
    disable_response = client.post("/registry/applications/overview/disable")
    assert disable_response.status_code == 200
    assert disable_response.json()["enabled"] is False

    enable_response = client.post("/registry/applications/overview/enable")

    assert enable_response.status_code == 200
    assert enable_response.json()["applicationId"] == "overview"
    assert enable_response.json()["enabled"] is True

    read_back = client.get("/registry/applications/overview")
    assert read_back.status_code == 200
    assert read_back.json()["enabled"] is True


def test_disable_endpoint_sets_enabled_false_and_returns_updated_payload(client) -> None:
    response = client.post("/registry/applications/overview/disable")

    assert response.status_code == 200
    assert response.json()["applicationId"] == "overview"
    assert response.json()["enabled"] is False

    read_back = client.get("/registry/applications/overview")
    assert read_back.status_code == 200
    assert read_back.json()["enabled"] is False


def test_enable_disable_endpoints_return_404_for_unknown_application(client) -> None:
    enable_response = client.post("/registry/applications/unknown-application/enable")
    disable_response = client.post("/registry/applications/unknown-application/disable")

    assert enable_response.status_code == 404
    assert disable_response.status_code == 404


def test_enable_disable_endpoints_record_application_audit_events(client) -> None:
    response = client.post("/registry/applications/overview/disable")
    assert response.status_code == 200

    with get_session_factory()() as session:
        event = (
            session.query(ApplicationAuditEvent)
            .filter(ApplicationAuditEvent.application_id == "overview")
            .order_by(ApplicationAuditEvent.id.desc())
            .first()
        )

        assert event is not None
        assert event.event_type == "disabled"
        assert event.message == "Application disabled via registry API"
        assert event.details_json == {
            "previous_enabled": True,
            "current_enabled": False,
        }
