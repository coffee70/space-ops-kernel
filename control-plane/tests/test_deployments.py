from __future__ import annotations

from pathlib import Path

import pytest


def test_phase3_fixture_service_can_scaffold_write_commit_deploy_and_delete(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Deployment, ManagedUnit

    unit_id = "phase3-test-fixture-service"
    branch = "feature/phase3-no-llm"
    main_path = f"project/space-ops-platform/backend/services/{unit_id}/app/main.py"
    requirements_path = f"project/space-ops-platform/backend/services/{unit_id}/requirements.txt"

    branch_response = client.post("/code/branches", json={"branch": branch, "from_branch": "main"})
    assert branch_response.status_code == 200

    scaffold_response = client.post(
        "/templates/python-service/scaffold",
        json={
            "branch": branch,
            "unit_id": unit_id,
            "display_name": "Phase 3 Test Fixture Service",
            "package_owner": "space-ops-platform",
            "source_path": f"project/space-ops-platform/backend/services/{unit_id}",
            "discovery": {
                "service_slug": unit_id,
                "capabilities": ["phase3-test-fixture"],
                "health_endpoint": "/health",
            },
        },
    )
    assert scaffold_response.status_code == 200

    requirements_response = client.put(
        "/code/file",
        json={
            "branch": branch,
            "path": requirements_path,
            "content": "fastapi>=0.109\nuvicorn[standard]>=0.27\n",
        },
    )
    assert requirements_response.status_code == 200

    main_response = client.put(
        "/code/file",
        json={
            "branch": branch,
            "path": main_path,
            "content": (
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {'status': 'ok', 'service': 'phase3-test-fixture-service'}\n"
                "@app.get('/metadata')\n"
                "def metadata():\n"
                "    return {'display_name': 'Phase 3 Test Fixture Service', 'mode': 'deterministic'}\n"
            ),
        },
    )
    assert main_response.status_code == 200

    commit_response = client.post(
        "/code/commits",
        json={"branch": branch, "message": "Add deterministic Phase 3 fixture service"},
    )
    assert commit_response.status_code == 200

    deploy_response = client.post("/deployments", json={"unit_id": unit_id, "branch": branch})
    assert deploy_response.status_code == 200
    deployment_payload = deploy_response.json()
    assert deployment_payload["status"] == "healthy"
    assert deployment_payload["registered"] is True

    registry = client.get("/registry/services")
    assert registry.status_code == 200
    service = next(item for item in registry.json() if item["unitId"] == unit_id)
    assert service["serviceSlug"] == unit_id
    assert service["deploymentStatus"] == "healthy"
    assert service["healthStatus"] == "passing"

    with get_session_factory()() as session:
        unit = session.get(ManagedUnit, unit_id)
        deployment = session.get(Deployment, deployment_payload["deployment_id"])
        assert unit is not None
        assert deployment is not None
        assert unit.delete_eligible is True
        assert deployment.delete_eligible is True
        assert deployment.runtime_ref is not None
        assert deployment.runtime_ref["health"]["path"] == "/health"

    delete_response = client.post("/internal/delete/managed-units", json={"unit_id": unit_id})
    assert delete_response.status_code == 200
    delete_payload = delete_response.json()
    assert any(item["resource_type"] == "managed_unit" and item["resource_id"] == unit_id for item in delete_payload["deleted"])
    assert client.get(f"/registry/services/{unit_id}").status_code == 404
    assert client.get(f"/internal/runtime-services/{unit_id}/health").status_code == 404

    repeat_delete = client.post("/internal/delete/managed-units", json={"unit_id": unit_id})
    assert repeat_delete.status_code == 200
    assert any(item["resource_type"] == "managed_unit" and item["resource_id"] == unit_id for item in repeat_delete.json()["already_absent"])


def test_successful_deployment_updates_registry(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Deployment

    response = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert response.status_code == 200
    deployment_payload = response.json()
    assert deployment_payload["status"] == "healthy"
    assert deployment_payload["registered"] is True

    with get_session_factory()() as session:
        deployment = session.get(Deployment, deployment_payload["deployment_id"])
        assert deployment is not None
        assert deployment.runtime_ref is not None
        assert deployment.runtime_ref["service_name"] == deployment.runtime_ref["transport"]["host"]
        assert deployment.runtime_ref["transport"]["port"] == 8080
        assert deployment.runtime_ref["proxy"]["base_path"] == ""
        assert deployment.runtime_ref["health"]["path"] == "/health"
        assert "target_url" not in deployment.runtime_ref
        assert "health_url" not in deployment.runtime_ref
        assert "base_url" not in deployment.runtime_ref

    registry = client.get("/registry/services")
    assert registry.status_code == 200
    services = registry.json()
    service = next(item for item in services if item["unitId"] == "derived-telemetry-service")
    assert service["serviceSlug"] == "derived-telemetry-service"
    assert service["deploymentStatus"] == "healthy"
    assert service["healthStatus"] == "passing"
    assert "runtime_endpoint" not in service
    assert "runtimeEndpoint" not in service
    assert "active_deployment_id" not in service
    assert "source_path" not in service
    assert "discovery_metadata_json" not in service


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
runtime_kind: service
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
    )
    assert commit_response.status_code == 200

    failed = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "feature/bad-manifest"})
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["registered"] is False

    registry = client.get("/registry/services")
    assert registry.status_code == 200
    service = next(item for item in registry.json() if item["unitId"] == "derived-telemetry-service")
    assert "active_deployment_id" not in service
    assert service["deploymentStatus"] == "healthy"


def test_redeployment_ignores_legacy_previous_runtime_ref_shape(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Deployment, ManagedUnit

    first = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert first.status_code == 200
    first_deployment_id = first.json()["deployment_id"]

    with get_session_factory()() as session:
        unit = session.get(ManagedUnit, "derived-telemetry-service")
        deployment = session.get(Deployment, first_deployment_id)
        assert unit is not None
        assert deployment is not None
        deployment.runtime_ref = {
            "service_name": deployment.runtime_ref["service_name"],
            "compose_file": deployment.runtime_ref["compose_file"],
            "env_file": deployment.runtime_ref["env_file"],
            "health_url": "http://legacy-runtime/health",
            "target_url": "http://legacy-runtime",
            "base_url": "http://legacy-runtime",
        }
        session.add(unit)
        session.add(deployment)
        session.commit()

    second = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert second.status_code == 200
    assert second.json()["status"] == "healthy"
    assert second.json()["deployment_id"] != first_deployment_id


def test_frontend_application_deployment_stores_structured_proxy_base_path(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Application, Deployment, ManagedUnit

    response = client.post("/deployments", json={"unit_id": "embedded-demo-application", "branch": "main"})
    assert response.status_code == 200
    deployment_id = response.json()["deployment_id"]

    with get_session_factory()() as session:
        deployment = session.get(Deployment, deployment_id)
        unit = session.get(ManagedUnit, "embedded-demo-application")
        application = session.get(Application, "embedded-demo")
        assert deployment is not None
        assert unit is not None
        assert application is not None
        assert deployment.runtime_ref is not None
        assert deployment.runtime_ref["service_name"] == deployment.runtime_ref["transport"]["host"]
        assert deployment.runtime_ref["transport"]["port"] == 3100
        assert deployment.runtime_ref["proxy"]["base_path"] == "/runtime-applications/embedded-demo"
        assert "target_url" not in deployment.runtime_ref
        assert unit.discovery_metadata_json == {}
        assert application.route_path == "/apps/embedded-demo"
        assert application.proxy_base_path == "/runtime-applications/embedded-demo"


def test_deployment_compose_uses_unit_source_root(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-apps" / "applications" / "embedded-demo-application"
    unit_root.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(get_settings(), object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="embedded-demo-application",
            display_name="Embedded Demo",
            package_owner="space-ops-apps",
            runtime_kind="frontend_application",
            runtime_template="frontend-embedded-application",
            source_path="project/space-ops-apps/applications/embedded-demo-application",
            build=BuildSpec(command="node --check server.js"),
            run=RunSpec(command="node server.js"),
            health=HealthSpec(type="http", path="/health", port=3100),
            discovery={},
            application={
                "application_id": "embedded-demo",
                "title": "Embedded Demo",
                "description": "Generic embedded runtime used to verify proxy-backed shell behavior.",
                "icon_key": "monitor-smartphone",
                "icon_color": "#38bdf8",
                "icon_background": "rgba(56, 189, 248, 0.16)",
                "application_type": "embedded",
                "route_path": "/apps/embedded-demo",
                "proxy_base_path": "/runtime-applications/embedded-demo",
                "version": "0.1.0",
                "enabled": True,
                "iframe_sandbox": "allow-scripts allow-same-origin allow-forms",
                "iframe_allow": "",
                "sort_order": 999,
                "capabilities": ["embedded-runtime-demo"],
            },
        ),
        source_root=source_root,
        service_name="embedded-demo-application-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    service = next(iter(payload["services"].values()))

    assert service["build"]["context"].endswith("/project/space-ops-apps/applications/embedded-demo-application")
    assert service["build"]["dockerfile"] == "Dockerfile"


def test_platform_node_service_deployment_uses_nested_source_root(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-platform" / "backend" / "services" / "agent-runtime-service"
    unit_root.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(get_settings(), object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="agent-runtime-service",
            display_name="Agent Runtime Service",
            package_owner="space-ops-platform",
            runtime_kind="service",
            runtime_template="node-service",
            source_path="project/space-ops-platform/backend/services/agent-runtime-service",
            build=BuildSpec(command="npm install && npm run build"),
            run=RunSpec(command="node dist/server.js"),
            health=HealthSpec(type="http", path="/health", port=8080),
            discovery={"service_slug": "agent-runtime-service"},
        ),
        source_root=source_root,
        service_name="agent-runtime-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    service = next(iter(payload["services"].values()))

    assert service["build"]["context"].endswith("/project/space-ops-platform/backend/services/agent-runtime-service")
    assert service["build"]["dockerfile"] == "Dockerfile"
    assert service["command"] == "node dist/server.js"


def test_vehicle_config_service_compose_mounts_apps_vehicle_configurations(
    control_plane_env: Path,
) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest, VolumeMountSpec

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-platform"
    unit_root.mkdir(parents=True, exist_ok=True)
    host_bundle = settings.workspace_root / "space-ops-apps" / "vehicle-configurations"
    host_bundle.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(settings, object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="vehicle-config-service",
            display_name="Vehicle Config Service",
            package_owner="space-ops-platform",
            runtime_kind="service",
            runtime_template="python-service",
            source_path="project/space-ops-platform",
            build=BuildSpec(command="pip install -r requirements.txt"),
            run=RunSpec(
                command=(
                    "sh -c \"cd /app/platform/backend && uvicorn main:app "
                    "--app-dir services/vehicle-config-service --host 0.0.0.0 --port 8080\""
                )
            ),
            health=HealthSpec(type="http", path="/health", port=8080),
            discovery={"service_slug": "vehicle-config-service"},
            mounts=[
                VolumeMountSpec(
                    source="space-ops-apps/vehicle-configurations",
                    target="/app/vehicle-configurations",
                    read_only=True,
                )
            ],
        ),
        source_root=source_root,
        service_name="vehicle-config-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview-vc.env",
    )
    spec = next(iter(payload["services"].values()))
    assert "volumes" in spec
    assert len(spec["volumes"]) == 1
    assert spec["volumes"][0].endswith(":/app/vehicle-configurations:ro")
    assert not spec["volumes"][0].startswith("/")
    assert "vehicle-configurations" in spec["volumes"][0]


def test_compose_mount_omitted_when_host_directory_missing(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest, VolumeMountSpec

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-platform").mkdir(parents=True, exist_ok=True)

    service = DeploymentService(settings, object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="vehicle-config-service",
            display_name="Vehicle Config Service",
            package_owner="space-ops-platform",
            runtime_kind="service",
            runtime_template="python-service",
            source_path="project/space-ops-platform",
            build=BuildSpec(command="pip install -r requirements.txt"),
            run=RunSpec(command="uvicorn app:app --host 0.0.0.0 --port 8080"),
            health=HealthSpec(type="http", path="/health", port=8080),
            discovery={"service_slug": "vehicle-config-service"},
            mounts=[
                VolumeMountSpec(
                    source="space-ops-apps/__missing_volume_dir__",
                    target="/app/data",
                    read_only=True,
                )
            ],
        ),
        source_root=source_root,
        service_name="vehicle-config-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview-vc.env",
    )
    spec = next(iter(payload["services"].values()))
    assert "volumes" not in spec


def test_compose_mount_read_write_uses_rw_suffix(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest, VolumeMountSpec

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-platform").mkdir(parents=True, exist_ok=True)
    data_dir = settings.workspace_root / "space-ops-apps" / "rw-mount-fixture"
    data_dir.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(settings, object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="fixture-service",
            display_name="Fixture",
            package_owner="space-ops-platform",
            runtime_kind="service",
            runtime_template="python-service",
            source_path="project/space-ops-platform",
            build=BuildSpec(command="pip install -r requirements.txt"),
            run=RunSpec(command="uvicorn app:app --host 0.0.0.0 --port 8080"),
            health=HealthSpec(type="http", path="/health", port=8080),
            discovery={"service_slug": "fixture-service"},
            mounts=[
                VolumeMountSpec(
                    source="space-ops-apps/rw-mount-fixture",
                    target="/app/writable",
                    read_only=False,
                )
            ],
        ),
        source_root=source_root,
        service_name="fixture-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview-rw.env",
    )
    spec = next(iter(payload["services"].values()))
    assert "volumes" in spec
    assert len(spec["volumes"]) == 1
    assert spec["volumes"][0].endswith(":/app/writable:rw")
    assert not spec["volumes"][0].startswith("/")
    assert "rw-mount-fixture" in spec["volumes"][0]


def test_volume_mount_spec_rejects_traversal_source() -> None:
    from pydantic import ValidationError

    from app.schemas import VolumeMountSpec

    with pytest.raises(ValidationError):
        VolumeMountSpec(source="space-ops-apps/../etc", target="/app/x", read_only=True)


def test_stub_runtime_ref_is_structured(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    settings = get_settings()
    settings.ensure_runtime_dirs()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-platform" / "backend" / "services" / "derived-telemetry-service"
    unit_root.mkdir(parents=True, exist_ok=True)
    logs_path = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-logs" / "preview.log"
    logs_path.parent.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(settings, object(), object())
    runtime_ref = service._deploy_runtime(
        deployment_id="dep_preview",
        manifest=UnitManifest(
            unit_id="derived-telemetry-service",
            display_name="Derived Telemetry Service",
            package_owner="space-ops-platform",
            runtime_kind="service",
            runtime_template="python-service",
            source_path="project/space-ops-platform/backend/services/derived-telemetry-service",
            build=BuildSpec(command="pip install -r requirements.txt"),
            run=RunSpec(command="uvicorn app.main:app --host 0.0.0.0 --port 8080"),
            health=HealthSpec(type="http", path="/health", port=8080),
            discovery={"service_slug": "derived-telemetry-service"},
        ),
        source_root=source_root,
        logs_path=logs_path,
    )

    assert runtime_ref.transport.scheme == "http"
    assert runtime_ref.transport.host == "derived-telemetry-service-dep-preview"
    assert runtime_ref.transport.port == 8080
    assert runtime_ref.health.path == "/health"
    assert runtime_ref.proxy.base_path == ""
    assert "target_url" not in runtime_ref.model_dump(mode="json")


def test_deployment_request_does_not_reimport_seed_source(client, control_plane_env) -> None:
    file_path = "project/space-ops-platform/backend/services/derived-telemetry-service/app/main.py"

    managed_content = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'status': 'managed-fork'}\n"
    )

    write_response = client.put(
        "/code/file",
        json={
            "branch": "main",
            "path": file_path,
            "content": managed_content,
        },
    )
    assert write_response.status_code == 200

    commit_response = client.post(
        "/code/commits",
        json={"branch": "main", "message": "Update deployable service"},
    )
    assert commit_response.status_code == 200
    commit_sha = commit_response.json()["commit_sha"]

    mounted_seed_file = (
        control_plane_env
        / "space-ops-platform"
        / "backend/services/derived-telemetry-service/app/main.py"
    )
    mounted_seed_file.write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def health():\n"
        "    return {'status': 'mounted-seed'}\n",
        encoding="utf-8",
    )

    deploy_response = client.post(
        "/deployments",
        json={
            "unit_id": "derived-telemetry-service",
            "branch": "main",
            "commit_sha": commit_sha,
        },
    )
    assert deploy_response.status_code == 200
    assert deploy_response.json()["commit_sha"] == commit_sha

    file_response = client.get(
        "/code/file",
        params={"branch": "main", "path": file_path},
    )
    assert file_response.status_code == 200
    assert "managed-fork" in file_response.json()["data"]["content"]
    assert "mounted-seed" not in file_response.json()["data"]["content"]
