from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


def _execute_queued_deployment(client, deployment_id: str) -> dict:
    from app.config import get_settings
    from app.deployments.worker import DeploymentWorker

    assert DeploymentWorker(get_settings()).run_once() == deployment_id
    response = client.get(f"/deployments/{deployment_id}")
    assert response.status_code == 200
    return response.json()


def _post_and_execute_deployment(client, payload: dict) -> dict:
    response = client.post("/deployments", json=payload)
    assert response.status_code == 200
    queued = response.json()
    assert queued["status"] == "queued"
    return _execute_queued_deployment(client, queued["deployment_id"])


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

    deployment_payload = _post_and_execute_deployment(client, {"unit_id": unit_id, "branch": branch})
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

    deployment_payload = _post_and_execute_deployment(client, {"unit_id": "derived-telemetry-service", "branch": "main"})
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


def test_deployment_submission_returns_queued_with_logs_before_worker(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Deployment

    response = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["health_status"] == "pending"
    assert payload["registered"] is False
    assert payload["logs_url"] == f"/deployments/{payload['deployment_id']}/logs"

    logs_response = client.get(payload["logs_url"])
    assert logs_response.status_code == 200
    logs = logs_response.json()["logs"]
    assert "Deployment queued" in logs
    assert f"deployment_id: {payload['deployment_id']}" in logs

    with get_session_factory()() as session:
        deployment = session.get(Deployment, payload["deployment_id"])
        assert deployment is not None
        assert deployment.runtime_ref is None
        assert deployment.build_started_at is None


def test_duplicate_submission_returns_existing_in_progress_deployment(client) -> None:
    first = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["status"] == "queued"

    second = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"})
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["deployment_id"] == first_payload["deployment_id"]
    assert second_payload["status"] == "queued"


def test_deployment_worker_claims_and_executes_oldest_queued_deployment(client) -> None:
    from app.config import get_settings
    from app.deployments.worker import DeploymentWorker

    first = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"}).json()
    second = client.post("/deployments", json={"unit_id": "model-registry-service", "branch": "main"}).json()

    worker = DeploymentWorker(get_settings())
    assert worker.run_once() == first["deployment_id"]

    first_status = client.get(f"/deployments/{first['deployment_id']}").json()
    second_status = client.get(f"/deployments/{second['deployment_id']}").json()
    assert first_status["status"] == "healthy"
    assert second_status["status"] == "queued"


def test_worker_startup_cleanup_marks_stale_deployments_failed(client) -> None:
    from datetime import timedelta

    from app.config import get_settings
    from app.db import get_session_factory
    from app.deployments.worker import DeploymentWorker
    from app.models.runtime import Deployment
    from app.registry.service import utcnow

    queued = client.post("/deployments", json={"unit_id": "derived-telemetry-service", "branch": "main"}).json()
    with get_session_factory()() as session:
        deployment = session.get(Deployment, queued["deployment_id"])
        assert deployment is not None
        deployment.requested_at = utcnow() - timedelta(minutes=30)
        session.commit()

    assert DeploymentWorker(get_settings()).cleanup_stale_jobs() == 1
    failed = client.get(f"/deployments/{queued['deployment_id']}").json()
    assert failed["status"] == "failed"
    assert "worker startup cleanup" in failed["failure_reason"]
    logs = client.get(f"/deployments/{queued['deployment_id']}/logs").json()["logs"]
    assert "worker startup cleanup" in logs


def test_failed_deployment_preserves_previous_healthy_state(client) -> None:
    first_payload = _post_and_execute_deployment(client, {"unit_id": "derived-telemetry-service", "branch": "main"})
    first_deployment_id = first_payload["deployment_id"]

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

    failed_payload = _post_and_execute_deployment(
        client,
        {"unit_id": "derived-telemetry-service", "branch": "feature/bad-manifest"},
    )
    assert failed_payload["status"] == "failed"
    assert failed_payload["registered"] is False

    registry = client.get("/registry/services")
    assert registry.status_code == 200
    service = next(item for item in registry.json() if item["unitId"] == "derived-telemetry-service")
    assert "active_deployment_id" not in service
    assert service["deploymentStatus"] == "healthy"


def test_redeployment_ignores_legacy_previous_runtime_ref_shape(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Deployment, ManagedUnit

    first_payload = _post_and_execute_deployment(client, {"unit_id": "derived-telemetry-service", "branch": "main"})
    first_deployment_id = first_payload["deployment_id"]

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

    second_payload = _post_and_execute_deployment(client, {"unit_id": "derived-telemetry-service", "branch": "main"})
    assert second_payload["status"] == "healthy"
    assert second_payload["deployment_id"] != first_deployment_id


def test_frontend_application_deployment_stores_structured_proxy_base_path(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import Application, Deployment, ManagedUnit

    branch = "feature/synthetic-embedded-app"
    assert client.post("/code/branches", json={"branch": branch, "from_branch": "main"}).status_code == 200
    scaffold_response = client.post(
        "/templates/frontend-embedded-application/scaffold",
        json={
            "branch": branch,
            "unit_id": "proxy-backed-test-application",
            "display_name": "Proxy Backed Test",
            "package_owner": "space-ops-apps",
            "discovery": {
                "application_id": "proxy-backed-test",
                "description": "Synthetic proxy-backed application fixture.",
            },
        },
    )
    assert scaffold_response.status_code == 200
    commit_response = client.post(
        "/code/commits",
        json={"branch": branch, "message": "Add synthetic embedded application fixture"},
    )
    assert commit_response.status_code == 200

    deployment_payload = _post_and_execute_deployment(client, {"unit_id": "proxy-backed-test-application", "branch": branch})
    deployment_id = deployment_payload["deployment_id"]

    with get_session_factory()() as session:
        deployment = session.get(Deployment, deployment_id)
        unit = session.get(ManagedUnit, "proxy-backed-test-application")
        application = session.get(Application, "proxy-backed-test")
        assert deployment is not None
        assert unit is not None
        assert application is not None
        assert deployment.runtime_ref is not None
        assert deployment.runtime_ref["service_name"] == deployment.runtime_ref["transport"]["host"]
        assert deployment.runtime_ref["transport"]["port"] == 3100
        assert deployment.runtime_ref["proxy"]["base_path"] == "/runtime-applications/proxy-backed-test"
        assert "target_url" not in deployment.runtime_ref
        assert unit.discovery_metadata_json == {}
        assert application.route_path == "/apps/proxy-backed-test"
        assert application.proxy_base_path == "/runtime-applications/proxy-backed-test"


def test_deployment_compose_uses_unit_source_root(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-apps" / "applications" / "proxy-backed-test-application"
    unit_root.mkdir(parents=True, exist_ok=True)

    service = DeploymentService(get_settings(), object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="proxy-backed-test-application",
            display_name="Proxy Backed Test",
            package_owner="space-ops-apps",
            runtime_kind="frontend_application",
            runtime_template="frontend-embedded-application",
            source_path="project/space-ops-apps/applications/proxy-backed-test-application",
            build=BuildSpec(command="node --check server.js"),
            run=RunSpec(command="node server.js"),
            health=HealthSpec(type="http", path="/health", port=3100),
            discovery={},
            application={
                "application_id": "proxy-backed-test",
                "title": "Proxy Backed Test",
                "description": "Synthetic embedded runtime fixture.",
                "icon_key": "monitor-smartphone",
                "icon_color": "#38bdf8",
                "icon_background": "rgba(56, 189, 248, 0.16)",
                "application_type": "embedded",
                "route_path": "/apps/proxy-backed-test",
                "proxy_base_path": "/runtime-applications/proxy-backed-test",
                "version": "0.1.0",
                "enabled": True,
                "iframe_sandbox": "allow-scripts allow-same-origin allow-forms",
                "iframe_allow": "",
                "sort_order": 999,
                "capabilities": ["embedded-runtime-demo"],
            },
        ),
        source_root=source_root,
        service_name="proxy-backed-test-application-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    service = next(iter(payload["services"].values()))

    assert service["build"]["context"].endswith("/project/space-ops-apps/applications/proxy-backed-test-application")
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


def test_failed_docker_compose_command_logs_captured_output(control_plane_env: Path, monkeypatch) -> None:
    from app.config import get_settings
    import app.deployments.service as deployment_service_module
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    settings = get_settings().model_copy(update={"runtime_strategy": "docker"})
    settings.generated_compose_root.mkdir(parents=True, exist_ok=True)
    settings.generated_env_root.mkdir(parents=True, exist_ok=True)
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "failed-preview" / "source"
    (source_root / "project" / "space-ops-apps" / "mission-control-ui").mkdir(parents=True, exist_ok=True)
    logs_path = settings.deployment_logs_root / "failed-preview.log"
    logs_path.parent.mkdir(parents=True, exist_ok=True)

    def fake_run_command(command: list[str], *, timeout: int | None = None):
        raise subprocess.CalledProcessError(
            17,
            command,
            output="docker build output\n> next build\n",
            stderr="Type error: Property 'missing' does not exist on type 'Props'.\n",
        )

    monkeypatch.setattr(deployment_service_module, "run_command", fake_run_command)
    service = DeploymentService(settings, object(), object())
    monkeypatch.setattr(service, "_compose_command", lambda: ["/usr/bin/docker-compose", "-p", "space-ops-kernel"])

    with pytest.raises(subprocess.CalledProcessError):
        service._deploy_runtime(
            deployment_id="failed-preview",
            manifest=UnitManifest(
                unit_id="mission-control-frontend-shell",
                display_name="Mission Control",
                package_owner="space-ops-apps",
                runtime_kind="frontend_shell",
                runtime_template="frontend-shell",
                source_path="project/space-ops-apps/mission-control-ui",
                build=BuildSpec(command="npm run build"),
                run=RunSpec(command="node server.js"),
                health=HealthSpec(type="http", path="/health", port=3000),
                discovery={},
            ),
            source_root=source_root,
            logs_path=logs_path,
        )

    logs = logs_path.read_text(encoding="utf-8")
    assert "Running docker compose up -d --build" in logs
    assert "Command: /usr/bin/docker-compose -p space-ops-kernel" in logs
    assert "Deployment command failed" in logs
    assert "exit_code: 17" in logs
    assert "docker build output" in logs
    assert "> next build" in logs
    assert "Type error: Property 'missing' does not exist on type 'Props'." in logs


def test_mission_control_frontend_shell_manifest_uses_standalone_server_command(control_plane_env: Path) -> None:
    import yaml

    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import UnitManifest

    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-apps" / "mission-control-ui").mkdir(parents=True, exist_ok=True)
    manifest_root = Path(__file__).resolve().parents[1]
    manifest = UnitManifest.model_validate(
        yaml.safe_load((manifest_root / "app/bootstrap/manifests/mission-control-frontend-shell.yaml").read_text(encoding="utf-8"))
    )

    payload = DeploymentService(get_settings(), object(), object())._build_compose_payload(
        manifest=manifest,
        source_root=source_root,
        service_name="mission-control-frontend-shell-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    service = next(iter(payload["services"].values()))

    assert manifest.runtime_kind == "frontend_shell"
    assert service["build"]["context"].endswith("/project/space-ops-apps/mission-control-ui")
    assert service["build"]["dockerfile"] == "Dockerfile"
    assert service["command"] == "node server.js"
    assert service["environment"]["PORT"] == "3000"
    assert service["environment"]["API_SERVER_URL"] == "http://telemetry-platform-edge-proxy:8080"
    assert service["environment"]["CONTROL_PLANE_SERVER_URL"] == "http://control-plane:8100"
    assert service["environment"]["NEXT_PUBLIC_API_URL"] == ""
    assert service["environment"]["NEXT_PUBLIC_CONTROL_PLANE_URL"] == ""


def test_compose_mounts_platform_vehicle_configurations(
    control_plane_env: Path,
) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest, VolumeMountSpec

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    unit_root = source_root / "project" / "space-ops-platform"
    unit_root.mkdir(parents=True, exist_ok=True)
    host_bundle = settings.workspace_root / "space-ops-platform" / "backend" / "resources" / "vehicle-configurations"
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
                    source="space-ops-platform/backend/resources/vehicle-configurations",
                    target="/app/platform/backend/resources/vehicle-configurations",
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
    assert spec["volumes"][0].endswith(":/app/platform/backend/resources/vehicle-configurations:ro")
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


def test_compose_named_volume_generates_service_mount_and_top_level_declaration(control_plane_env: Path) -> None:
    from app.config import Settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, NamedVolumeMountSpec, RunSpec, UnitManifest

    workspace_root = control_plane_env
    settings = Settings(
        database_url="postgresql://u:p@localhost:5432/db",
        workspace_root=workspace_root,
        runtime_root=workspace_root / "space-ops-kernel" / "runtime",
    )
    source_root = workspace_root / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-platform").mkdir(parents=True, exist_ok=True)

    service = DeploymentService(settings, object(), object())
    payload = service._build_compose_payload(
        manifest=UnitManifest(
            unit_id="model-registry-service",
            display_name="Model Registry Service",
            package_owner="space-ops-platform",
            runtime_kind="service",
            runtime_template="node-service",
            source_path="project/space-ops-platform",
            build=BuildSpec(command="pip install -r requirements.txt"),
            run=RunSpec(command="node dist/server.js"),
            health=HealthSpec(type="http", path="/health", port=8080),
            discovery={"service_slug": "model-registry-service"},
            named_volumes=[
                NamedVolumeMountSpec(
                    name="model_registry_data",
                    target="/app/model-registry",
                    read_only=False,
                )
            ],
        ),
        source_root=source_root,
        service_name="model-registry-service-preview",
        env_path=workspace_root / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    spec = next(iter(payload["services"].values()))
    assert spec["volumes"] == ["model_registry_data:/app/model-registry:rw"]
    assert payload["volumes"] == {"model_registry_data": {}}


def test_compose_bind_mounts_and_named_volumes_can_coexist(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, NamedVolumeMountSpec, RunSpec, UnitManifest, VolumeMountSpec

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-platform").mkdir(parents=True, exist_ok=True)
    data_dir = settings.workspace_root / "space-ops-apps" / "coexist-mount-fixture"
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
                    source="space-ops-apps/coexist-mount-fixture",
                    target="/app/bind",
                    read_only=True,
                )
            ],
            named_volumes=[
                NamedVolumeMountSpec(
                    name="fixture_data",
                    target="/app/data",
                    read_only=False,
                )
            ],
        ),
        source_root=source_root,
        service_name="fixture-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )
    spec = next(iter(payload["services"].values()))
    assert len(spec["volumes"]) == 2
    assert spec["volumes"][0].endswith(":/app/bind:ro")
    assert spec["volumes"][1] == "fixture_data:/app/data:rw"
    assert payload["volumes"] == {"fixture_data": {}}


def test_model_registry_manifest_compose_uses_named_volume(control_plane_env: Path) -> None:
    import yaml

    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import UnitManifest

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-platform" / "backend" / "services" / "model-registry-service").mkdir(
        parents=True, exist_ok=True
    )
    manifest_root = Path(__file__).resolve().parents[1]
    manifest = UnitManifest.model_validate(
        yaml.safe_load((manifest_root / "app/bootstrap/manifests/model-registry-service.yaml").read_text(encoding="utf-8"))
    )

    payload = DeploymentService(settings, object(), object())._build_compose_payload(
        manifest=manifest,
        source_root=source_root,
        service_name="model-registry-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )

    spec = next(iter(payload["services"].values()))
    assert spec["volumes"] == ["model_registry_data:/app/model-registry:rw"]
    assert payload["volumes"] == {"model_registry_data": {}}


def test_vehicle_config_manifest_compose_uses_named_volume(control_plane_env: Path) -> None:
    import yaml

    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import UnitManifest

    settings = get_settings()
    source_root = control_plane_env / "space-ops-kernel" / "runtime" / "deployment-workspaces" / "preview" / "source"
    (source_root / "project" / "space-ops-platform").mkdir(parents=True, exist_ok=True)
    manifest_root = Path(__file__).resolve().parents[1]
    manifest = UnitManifest.model_validate(
        yaml.safe_load((manifest_root / "app/bootstrap/manifests/vehicle-config-service.yaml").read_text(encoding="utf-8"))
    )

    payload = DeploymentService(settings, object(), object())._build_compose_payload(
        manifest=manifest,
        source_root=source_root,
        service_name="vehicle-config-service-preview",
        env_path=control_plane_env / "space-ops-kernel" / "runtime" / "generated" / "env" / "preview.env",
    )

    spec = next(iter(payload["services"].values()))
    assert spec["volumes"] == ["vehicle_config_data:/app/vehicle-configurations:rw"]
    assert payload["volumes"] == {"vehicle_config_data": {}}


def test_volume_mount_spec_rejects_traversal_source() -> None:
    from pydantic import ValidationError

    from app.schemas import VolumeMountSpec

    with pytest.raises(ValidationError):
        VolumeMountSpec(source="space-ops-apps/../etc", target="/app/x", read_only=True)


def test_named_volume_mount_spec_validates_compose_safe_fields() -> None:
    from app.schemas import NamedVolumeMountSpec

    spec = NamedVolumeMountSpec(name="model_registry_data", target="/app/model-registry", read_only=False)

    assert spec.name == "model_registry_data"
    assert spec.target == "/app/model-registry"
    assert spec.read_only is False


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("ModelRegistry", "/app/model-registry"),
        ("model.registry", "/app/model-registry"),
        ("model_registry_data", "app/model-registry"),
        ("model_registry_data", "/app/../model-registry"),
    ],
)
def test_named_volume_mount_spec_rejects_invalid_fields(name: str, target: str) -> None:
    from pydantic import ValidationError

    from app.schemas import NamedVolumeMountSpec

    with pytest.raises(ValidationError):
        NamedVolumeMountSpec(name=name, target=target, read_only=False)


def test_satnogs_env_only_injected_for_satnogs_adapter_service(control_plane_env: Path) -> None:
    from app.config import get_settings
    from app.deployments.service import DeploymentService
    from app.schemas import BuildSpec, HealthSpec, RunSpec, UnitManifest

    settings = get_settings()
    service = DeploymentService(settings, object(), object())
    common_manifest = UnitManifest(
        unit_id="telemetry-ingest-service",
        display_name="Telemetry Ingest Service",
        package_owner="space-ops-platform",
        runtime_kind="service",
        runtime_template="python-service",
        source_path="project/space-ops-platform",
        build=BuildSpec(command="pip install -r requirements.txt"),
        run=RunSpec(command="uvicorn main:app"),
        health=HealthSpec(type="http", path="/health", port=8080),
        discovery={"service_slug": "telemetry-ingest-service"},
    )
    adapter_manifest = common_manifest.model_copy(update={"unit_id": "satnogs-adapter-service"})
    simulator_manifest = common_manifest.model_copy(update={"unit_id": "simulator-service"})
    simulator_2_manifest = common_manifest.model_copy(update={"unit_id": "simulator-2-service"})

    common_env = service._build_runtime_env(common_manifest, "telemetry-ingest-service-dep-test")
    adapter_env = service._build_runtime_env(adapter_manifest, "satnogs-adapter-service-dep-test")
    simulator_env = service._build_runtime_env(simulator_manifest, "simulator-service-dep-test")
    simulator_2_env = service._build_runtime_env(simulator_2_manifest, "simulator-2-service-dep-test")

    assert "SATNOGS_API_TOKEN" not in common_env
    assert "SATNOGS_LIVE_ENABLED" not in common_env
    assert "SATNOGS_ADAPTER_CONFIG" not in common_env
    assert "SATNOGS_DLQ_ROOT" not in common_env
    assert adapter_env["SATNOGS_API_TOKEN"] == settings.platform_satnogs_api_token
    assert adapter_env["SATNOGS_LIVE_ENABLED"] == settings.platform_satnogs_live_enabled
    assert adapter_env["SATNOGS_ADAPTER_CONFIG"] == settings.platform_satnogs_adapter_config
    assert adapter_env["SATNOGS_DLQ_ROOT"] == settings.platform_satnogs_dlq_root
    assert "VEHICLE_CONFIG_PATH" not in common_env
    assert "BACKEND_URL" not in common_env
    assert "API_SERVER_URL" not in common_env
    assert "CONTROL_PLANE_SERVER_URL" not in common_env
    assert "NEXT_PUBLIC_API_URL" not in common_env
    assert "NEXT_PUBLIC_CONTROL_PLANE_URL" not in common_env
    assert simulator_env["BACKEND_URL"] == settings.platform_api_base_url
    assert simulator_env["VEHICLE_CONFIG_PATH"] == "simulators/drogonsat.yaml"
    assert simulator_2_env["BACKEND_URL"] == settings.platform_api_base_url
    assert simulator_2_env["VEHICLE_CONFIG_PATH"] == "simulators/rhaegalsat.json"


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
