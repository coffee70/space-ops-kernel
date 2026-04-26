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
    assert set(service) == {
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


def test_registry_service_response_does_not_expose_runtime_internals(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "telemetry-ingest-service", "branch": "main"})
    assert deployment.status_code == 200

    response = client.get("/registry/services/telemetry-ingest-service")

    assert response.status_code == 200
    payload = response.json()
    assert FORBIDDEN_PUBLIC_FIELDS.isdisjoint(payload)

