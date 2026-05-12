from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import get_session_factory
from app.models.runtime import Application, ApplicationAuditEvent


def _application_row(application_id: str, **overrides) -> Application:
    values = {
        "application_id": application_id,
        "title": f"Test {application_id}",
        "description": "Test application registry row.",
        "icon_key": "test",
        "icon_color": "#ffffff",
        "icon_background": "rgba(255,255,255,0.16)",
        "application_type": "native",
        "route_path": f"/apps/{application_id}",
        "loader_key": application_id,
        "embedded_url": None,
        "proxy_base_path": None,
        "version": "0.1.0",
        "enabled": True,
        "sort_order": 100,
        "owner": "tests",
        "health_status": "unknown",
        "deployment_status": "seeded",
    }
    values.update(overrides)
    return Application(**values)


def _commit_application(application: Application) -> None:
    with get_session_factory()() as session:
        session.add(application)
        session.commit()


def _assert_application_row_fails(application: Application) -> None:
    with pytest.raises(IntegrityError):
        _commit_application(application)


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
        "control-panel",
        "ai-engineer",
    ]


def test_get_registry_application_returns_404_for_unknown_application(client) -> None:
    response = client.get("/registry/applications/unknown-application")

    assert response.status_code == 404


def test_application_registry_db_rejects_invalid_transport_contracts(client) -> None:
    invalid_rows = [
        _application_row("db-native-no-loader", loader_key=None),
        _application_row("db-native-embedded-url", embedded_url="/_embedded/native"),
        _application_row(
            "db-embedded-loader",
            application_type="embedded",
            loader_key="db-embedded-loader",
            embedded_url="/_embedded/loader",
        ),
        _application_row(
            "db-embedded-no-transport",
            application_type="embedded",
            loader_key=None,
        ),
    ]

    for application in invalid_rows:
        _assert_application_row_fails(application)


def test_application_registry_db_rejects_route_path_mismatch(client) -> None:
    _assert_application_row_fails(
        _application_row("db-route-mismatch", route_path="/apps/other-application"),
    )


def test_application_registry_db_rejects_invalid_proxy_base_path(client) -> None:
    _assert_application_row_fails(
        _application_row(
            "db-bad-proxy",
            application_type="embedded",
            loader_key=None,
            proxy_base_path="/not-runtime/foo",
        ),
    )


def test_application_registry_db_accepts_internal_embedded_transport(client) -> None:
    _commit_application(
        _application_row(
            "db-internal-embedded",
            application_type="embedded",
            loader_key=None,
            embedded_url="/_embedded/internal-test-app",
        ),
    )


def test_application_registry_db_accepts_proxy_backed_embedded_transport(client) -> None:
    _commit_application(
        _application_row(
            "db-proxy-demo",
            application_type="embedded",
            loader_key=None,
            proxy_base_path="/runtime-applications/db-proxy-demo",
        ),
    )


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
