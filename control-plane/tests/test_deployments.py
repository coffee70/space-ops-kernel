from __future__ import annotations

from pathlib import Path


def test_successful_deployment_updates_registry(client) -> None:
    response = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert response.status_code == 200
    deployment = response.json()
    assert deployment["status"] == "healthy"
    assert deployment["registered"] is True

    registry = client.get("/registry/services")
    assert registry.status_code == 200
    services = registry.json()
    assert services[0]["unit_id"] == "derived-telemetry-service"
    assert services[0]["deployment_status"] == "healthy"


def test_failed_deployment_preserves_previous_healthy_state(client) -> None:
    first = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert first.status_code == 200
    first_deployment_id = first.json()["deployment_id"]

    create_branch = client.post("/code/branches", json={"branch": "feature/bad-manifest", "from_branch": "main"})
    assert create_branch.status_code == 200

    bad_manifest = """
unit_id: derived-telemetry-service
display_name: Derived Telemetry Service
package_owner: space-ops-platform
unit_kind: service
runtime_template: invalid-template
source_path: project/space-ops-platform/backend/services/derived-telemetry-service
build:
  command: pip install -r requirements.txt
run:
  command: uvicorn app.main:app --host 0.0.0.0 --port 8080
health:
  type: http
  path: /health
  port: 8080
discovery:
  category: telemetry
"""
    write_response = client.put(
        "/code/file",
        json={
            "branch": "feature/bad-manifest",
            "path": "manifests/units/derived-telemetry-service.yaml",
            "content": bad_manifest,
        },
    )
    assert write_response.status_code == 200

    commit_response = client.post(
        "/code/commits",
        json={"branch": "feature/bad-manifest", "message": "Break manifest"},
        headers={"X-Actor-Id": "operator", "X-Actor-Name": "Operator"},
    )
    assert commit_response.status_code == 200

    failed = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "feature/bad-manifest"})
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["registered"] is False

    registry = client.get("/registry/services")
    assert registry.status_code == 200
    service = registry.json()[0]
    assert service["active_deployment_id"] == first_deployment_id
    assert service["deployment_status"] == "healthy"


def test_deployment_compose_uses_unit_source_root(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-apps" / "modules" / "battery-efficiency-module"
    unit_root.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(get_settings(), object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="battery-efficiency-module",
            display_name="Battery Efficiency",
            package_owner="space-ops-apps",
            unit_kind="module",
            runtime_template="frontend-module",
            source_path="project/space-ops-apps/modules/battery-efficiency-module",
            build=BuildSpec(command="node --check server.js"),
            run=RunSpec(command="node server.js"),
            health=HealthSpec(type="http", path="/health", port=3100),
            discovery={"route_slug": "battery-efficiency"},
        ),
        source_root=source_root,
        service_name="battery-efficiency-module-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    service = next(iter(payload["services"].values()))

    assert service["build"]["context"].endswith("/project/space-ops-apps/modules/battery-efficiency-module")
    assert service["build"]["dockerfile"] == "Dockerfile"
