from __future__ import annotations


# `sourcePath` is intentionally surfaced on the unit summary so the change-
# preview aggregator can map changed file paths to a target unit without
# hardcoding service-specific directories. It is plain metadata describing
# where a unit's source lives in the managed fork, not a runtime topology
# leak.
FORBIDDEN_PUBLIC_FIELDS = {
    "runtime_endpoint",
    "runtimeEndpoint",
    "runtime_ref",
    "runtimeRef",
    "host",
    "port",
    "active_deployment_id",
    "activeDeploymentId",
    "discovery_metadata_json",
    "discoveryMetadataJson",
    "deployment_id",
    "deploymentId",
}


def test_registry_units_returns_safe_unit_catalog(client) -> None:
    response = client.get("/registry/units")

    assert response.status_code == 200
    payload = response.json()
    unit = next(item for item in payload if item["unitId"] == "telemetry-ingest-service")
    assert set(unit) == {
        "unitId",
        "displayName",
        "packageOwner",
        "runtimeKind",
        "runtimeTemplate",
        "deploymentStatus",
        "healthStatus",
        "serviceSlug",
        "applicationId",
        "sourcePath",
        "capabilities",
        "category",
        "description",
    }
    assert unit["sourcePath"]


def test_registry_units_response_does_not_expose_runtime_or_deployment_internals(client) -> None:
    deployment = client.post("/deployments", json={"unit_id": "telemetry-ingest-service", "branch": "main"})
    assert deployment.status_code == 200

    response = client.get("/registry/units")

    assert response.status_code == 200
    for unit in response.json():
        assert FORBIDDEN_PUBLIC_FIELDS.isdisjoint(unit)

