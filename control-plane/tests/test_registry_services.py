from __future__ import annotations


FORBIDDEN_PUBLIC_FIELDS = {
    "runtime_endpoint",
    "runtimeEndpoint",
    "runtime_ref",
    "runtimeRef",
    "host",
    "port",
    "active_deployment_id",
    "activeDeploymentId",
    "source_path",
    "sourcePath",
    "discovery_metadata_json",
    "discoveryMetadataJson",
    "deployment_id",
    "deploymentId",
}


def test_registry_services_returns_safe_service_catalog(client) -> None:
    response = client.get("/registry/services")

    assert response.status_code == 200
    payload = response.json()
    service = next(item for item in payload if item["serviceSlug"] == "telemetry-ingest-service")
    required = {
        "serviceSlug",
        "unitId",
        "displayName",
        "packageOwner",
        "runtimeKind",
        "runtimeTemplate",
        "deploymentStatus",
        "healthStatus",
        "category",
        "description",
        "capabilities",
    }
    optional_deployment = {"branch", "commitSha"}
    keys = set(service)
    assert keys >= required
    assert keys <= required | {"runtimeTarget"} | optional_deployment
    unknown = keys - required - {"runtimeTarget"} - optional_deployment
    assert not unknown


def test_telemetry_ingest_runtime_transport_uses_health_port_8080(client) -> None:
    from app.db import get_session_factory
    from app.registry.service import RegistryService

    deployment = client.post("/deployments", json={"unit_id": "telemetry-ingest-service", "branch": "main"})
    assert deployment.status_code == 200

    response = client.get("/registry/services?includeRuntimeTransport=true")

    assert response.status_code == 200
    payload = response.json()
    ingest = next(item for item in payload if item["serviceSlug"] == "telemetry-ingest-service")
    rt = ingest.get("runtimeTarget")
    assert rt is not None
    assert rt["port"] == 8080
    assert rt["healthPath"] == "/health"
    host = rt.get("host")
    assert isinstance(host, str) and len(host) > 0

    with get_session_factory()() as session:
        registry = RegistryService(session)
        ref = registry.get_runtime_ref_for_unit("telemetry-ingest-service")
        assert ref is not None
        assert ref.transport.port == 8080
        assert ref.health.path == "/health"
        assert rt["host"] == ref.transport.host
        assert rt["serviceName"] == ref.service_name


def test_registry_service_response_does_not_expose_runtime_internals(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "telemetry-ingest-service", "branch": "main"})
    assert deployment.status_code == 200

    response = client.get("/registry/services/telemetry-ingest-service")

    assert response.status_code == 200
    payload = response.json()
    assert FORBIDDEN_PUBLIC_FIELDS.isdisjoint(payload)


def test_registry_services_normalizes_capability_tags_to_capabilities(client) -> None:
    response = client.get("/registry/services/telemetry-ingest-service")
    assert response.status_code == 200
    payload = response.json()
    assert "capabilities" in payload
    assert "ingest" in payload["capabilities"]

