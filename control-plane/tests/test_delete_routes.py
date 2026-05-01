from __future__ import annotations

from datetime import timedelta
from pathlib import Path


def _runtime_ref(runtime_root: Path, deployment_id: str, service_name: str) -> dict:
    return {
        "service_name": service_name,
        "compose_file": str(runtime_root / "generated" / "compose" / f"{deployment_id}.yaml"),
        "env_file": str(runtime_root / "generated" / "env" / f"{deployment_id}.env"),
        "transport": {"scheme": "http", "host": service_name, "port": 8080},
        "health": {"path": "/health"},
        "proxy": {"base_path": ""},
    }


def _add_unit(session, *, unit_id: str, deployment_id: str | None = None, delete_eligible: bool = True):
    from app.models.runtime import ManagedUnit

    unit = ManagedUnit(
        unit_id=unit_id,
        display_name=unit_id.replace("-", " ").title(),
        package_owner="space-ops-platform",
        runtime_kind="service",
        runtime_template="python-service",
        source_path=f"project/space-ops-platform/backend/services/{unit_id}",
        deployment_status="healthy",
        health_status="passing",
        discovery_metadata_json={"service_slug": unit_id},
        active_deployment_id=deployment_id,
        delete_eligible=delete_eligible,
    )
    session.add(unit)
    session.flush()
    return unit


def _add_deployment(
    session,
    *,
    runtime_root: Path,
    unit_id: str,
    deployment_id: str,
    delete_eligible: bool = True,
):
    from app.models.runtime import Deployment

    deployment = Deployment(
        deployment_id=deployment_id,
        unit_id=unit_id,
        branch="main",
        commit_sha="abc1234",
        status="healthy",
        health_status="passing",
        artifact_ref=str(runtime_root / "artifacts" / deployment_id),
        runtime_ref=_runtime_ref(runtime_root, deployment_id, f"{unit_id}-{deployment_id}"),
        delete_eligible=delete_eligible,
    )
    session.add(deployment)
    session.flush()
    return deployment


def _add_application(session, *, application_id: str = "delete-app", delete_eligible: bool = True):
    from app.models.runtime import Application

    application = Application(
        application_id=application_id,
        title="Delete App",
        description="Delete test application.",
        icon_key="app-window",
        icon_color="#38bdf8",
        icon_background="rgba(56, 189, 248, 0.16)",
        application_type="embedded",
        route_path=f"/apps/{application_id}",
        embedded_url=None,
        proxy_base_path=f"/runtime-applications/{application_id}",
        version="0.1.0",
        enabled=True,
        sort_order=100,
        owner="space-ops-platform",
        health_status="passing",
        deployment_status="healthy",
        delete_eligible=delete_eligible,
    )
    session.add(application)
    session.flush()
    return application


def test_delete_managed_unit_removes_records_files_and_records_events(client, control_plane_env) -> None:
    from app.config import get_settings
    from app.db import get_session_factory
    from app.models.runtime import Deployment, DeploymentEvent, ManagedUnit, ResourceDeleteEvent, UnitHealthSnapshot

    runtime_root = get_settings().resolved_runtime_root
    deployment_id = "dep_deleteable"
    for path in (
        runtime_root / "generated" / "compose" / f"{deployment_id}.yaml",
        runtime_root / "generated" / "env" / f"{deployment_id}.env",
        runtime_root / "deployment-logs" / f"{deployment_id}.log",
        runtime_root / "deployment-workspaces" / deployment_id / "source.txt",
        runtime_root / "artifacts" / deployment_id / "artifact.txt",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("delete me", encoding="utf-8")

    with get_session_factory()() as session:
        _add_unit(session, unit_id="deleteable-service", deployment_id=deployment_id)
        _add_deployment(session, runtime_root=runtime_root, unit_id="deleteable-service", deployment_id=deployment_id)
        session.add(DeploymentEvent(deployment_id=deployment_id, event_type="healthy", message="ok"))
        session.add(UnitHealthSnapshot(unit_id="deleteable-service", deployment_id=deployment_id, status="passing"))
        session.commit()

    registry_before = client.get("/registry/services")
    assert registry_before.status_code == 200
    assert any(item["unitId"] == "deleteable-service" for item in registry_before.json())

    response = client.post("/internal/delete/managed-units", json={"unit_id": "deleteable-service"})
    assert response.status_code == 200
    payload = response.json()
    assert any(item["resource_type"] == "managed_unit" for item in payload["deleted"])
    assert any(
        item["resource_type"] == "deployment_workspace"
        for item in [*payload["removed"], *payload["already_absent"]]
    )
    assert any(item["resource_type"] == "deployment_runtime" for item in payload["skipped"])

    assert not (runtime_root / "generated" / "compose" / f"{deployment_id}.yaml").exists()
    assert not (runtime_root / "deployment-workspaces" / deployment_id).exists()
    with get_session_factory()() as session:
        assert session.get(ManagedUnit, "deleteable-service") is None
        assert session.get(Deployment, deployment_id) is None
        events = session.query(ResourceDeleteEvent).filter(ResourceDeleteEvent.resource_id == "deleteable-service").all()
        assert events
    registry_after = client.get("/registry/services")
    assert registry_after.status_code == 200
    assert all(item["unitId"] != "deleteable-service" for item in registry_after.json())
    assert client.get("/registry/services/deleteable-service").status_code == 404
    assert client.get("/internal/runtime-services/deleteable-service/health").status_code == 404

    repeat = client.post("/internal/delete/managed-units", json={"unit_id": "deleteable-service"})
    assert repeat.status_code == 200
    assert any(item["resource_type"] == "managed_unit" for item in repeat.json()["already_absent"])


def test_delete_refuses_non_delete_eligible_unit(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import ManagedUnit

    with get_session_factory()() as session:
        session.add(
            ManagedUnit(
                unit_id="protected-service",
                display_name="Protected Service",
                package_owner="space-ops-platform",
                runtime_kind="service",
                runtime_template="python-service",
                source_path="project/space-ops-platform/backend/services/protected-service",
                deployment_status="healthy",
                health_status="passing",
                discovery_metadata_json={},
                delete_eligible=False,
            )
        )
        session.commit()

    response = client.post("/internal/delete/managed-units", json={"unit_id": "protected-service"})
    assert response.status_code == 200
    assert response.json()["refused"][0]["reason"] == "managed unit delete_eligible is false"
    with get_session_factory()() as session:
        assert session.get(ManagedUnit, "protected-service") is not None


def test_delete_managed_branch_removes_worktree_and_is_idempotent(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import ManagedBranch

    create = client.post("/code/branches", json={"branch": "feature/delete-me", "from_branch": "main"})
    assert create.status_code == 200
    with get_session_factory()() as session:
        branch = session.query(ManagedBranch).filter(ManagedBranch.branch_name == "feature/delete-me").one()
        worktree_path = branch.worktree_path
        assert branch.delete_eligible is True

    response = client.post("/internal/delete/code", json={"branch": "feature/delete-me"})
    assert response.status_code == 200
    payload = response.json()
    assert any(item["resource_type"] == "branch_ref" for item in payload["removed"])
    assert any(item["resource_type"] == "branch_worktree" for item in payload["removed"])
    assert not __import__("pathlib").Path(worktree_path).exists()

    repeat = client.post("/internal/delete/code", json={"branch": "feature/delete-me"})
    assert repeat.status_code == 200
    assert any(item["resource_type"] == "branch_ref" for item in repeat.json()["already_absent"])


def test_delete_code_reports_unsafe_paths_as_refused(client) -> None:
    create = client.post("/code/branches", json={"branch": "feature/path-refusal", "from_branch": "main"})
    assert create.status_code == 200

    response = client.post(
        "/internal/delete/code",
        json={"branch": "feature/path-refusal", "paths": ["../secrets", "https://example.test/x", "project/*"]},
    )
    assert response.status_code == 200
    refused = response.json()["refused"]
    assert {item["resource_id"] for item in refused} >= {"../secrets", "https://example.test/x", "project/*"}


def test_delete_api_routes_are_limited_to_supported_operations(client) -> None:
    from app.config import get_settings
    from app.db import get_session_factory
    from app.models.runtime import ManagedUnit, utcnow
    from app.main import app

    with get_session_factory()() as session:
        unit = _add_unit(session, unit_id="stale-deleteable-service", delete_eligible=True)
        unit.created_at = utcnow() - timedelta(hours=4)
        session.commit()

    delete_routes = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/internal/delete")
        and "POST" in getattr(route, "methods", set())
    }
    assert delete_routes == {
        "/internal/delete/managed-units",
        "/internal/delete/code",
        "/internal/delete/stale",
    }

    with get_session_factory()() as session:
        assert session.get(ManagedUnit, "stale-deleteable-service") is not None

    stale_response = client.post("/internal/delete/stale", json={"older_than_minutes": 120})
    assert stale_response.status_code == 200
    with get_session_factory()() as session:
        assert session.get(ManagedUnit, "stale-deleteable-service") is None


def test_existing_branch_is_not_marked_delete_eligible_by_branch_name(client) -> None:
    from app.config import get_settings
    from app.db import get_session_factory
    from app.git.repository import ManagedGitRepository
    from app.models.runtime import ManagedBranch

    repository = ManagedGitRepository(get_settings())
    repository.create_branch("feature/preexisting", "main")

    response = client.post("/code/branches", json={"branch": "feature/preexisting", "from_branch": "main"})
    assert response.status_code == 200
    with get_session_factory()() as session:
        branch = session.query(ManagedBranch).filter(ManagedBranch.branch_name == "feature/preexisting").one()
        assert branch.delete_eligible is False

    delete_response = client.post("/internal/delete/code", json={"branch": "feature/preexisting"})
    assert delete_response.status_code == 200
    assert delete_response.json()["refused"][0]["reason"] == "managed branch delete_eligible is false"


def test_existing_managed_branch_preserves_delete_eligible_value(client) -> None:
    from app.db import get_session_factory
    from app.models.runtime import ManagedBranch

    create = client.post("/code/branches", json={"branch": "feature/preserve-delete-eligible", "from_branch": "main"})
    assert create.status_code == 200
    with get_session_factory()() as session:
        branch = session.query(ManagedBranch).filter(ManagedBranch.branch_name == "feature/preserve-delete-eligible").one()
        branch.delete_eligible = False
        session.commit()

    recreate = client.post("/code/branches", json={"branch": "feature/preserve-delete-eligible", "from_branch": "main"})
    assert recreate.status_code == 200
    with get_session_factory()() as session:
        branch = session.query(ManagedBranch).filter(ManagedBranch.branch_name == "feature/preserve-delete-eligible").one()
        assert branch.delete_eligible is False


def test_managed_unit_delete_refuses_when_any_child_deployment_is_protected(client) -> None:
    from app.config import get_settings
    from app.db import get_session_factory
    from app.models.runtime import Deployment, ManagedUnit

    runtime_root = get_settings().resolved_runtime_root
    protected_file = runtime_root / "generated" / "compose" / "dep_mixed_ok.yaml"
    protected_file.parent.mkdir(parents=True, exist_ok=True)
    protected_file.write_text("delete me only if fully authorized", encoding="utf-8")

    with get_session_factory()() as session:
        _add_unit(session, unit_id="mixed-service", deployment_id="dep_mixed_ok")
        _add_deployment(session, runtime_root=runtime_root, unit_id="mixed-service", deployment_id="dep_mixed_ok")
        _add_deployment(
            session,
            runtime_root=runtime_root,
            unit_id="mixed-service",
            deployment_id="dep_mixed_protected",
            delete_eligible=False,
        )
        session.commit()

    response = client.post("/internal/delete/managed-units", json={"unit_id": "mixed-service"})
    assert response.status_code == 200
    payload = response.json()
    assert any(item["resource_id"] == "dep_mixed_protected" for item in payload["refused"])
    assert not payload["deleted"]
    assert protected_file.exists()
    with get_session_factory()() as session:
        assert session.get(ManagedUnit, "mixed-service") is not None
        assert session.get(Deployment, "dep_mixed_ok") is not None
        assert session.get(Deployment, "dep_mixed_protected") is not None


def test_managed_unit_delete_refuses_protected_application_deployment(client) -> None:
    from app.config import get_settings
    from app.db import get_session_factory
    from app.models.runtime import ApplicationDeployment, Deployment, ManagedUnit

    runtime_root = get_settings().resolved_runtime_root
    with get_session_factory()() as session:
        _add_unit(session, unit_id="app-owned-service", deployment_id="dep_app_protected")
        _add_deployment(session, runtime_root=runtime_root, unit_id="app-owned-service", deployment_id="dep_app_protected")
        _add_application(session, application_id="protected-app")
        session.add(
            ApplicationDeployment(
                deployment_id="dep_app_protected",
                application_id="protected-app",
                commit_sha="abc1234",
                artifact_ref=None,
                runtime_ref=_runtime_ref(runtime_root, "dep_app_protected", "app-owned-service-dep-app-protected"),
                status="healthy",
                health_status="passing",
                delete_eligible=False,
            )
        )
        session.commit()

    response = client.post("/internal/delete/managed-units", json={"unit_id": "app-owned-service"})
    assert response.status_code == 200
    payload = response.json()
    assert any(item["resource_type"] == "application_deployment" for item in payload["refused"])
    assert not payload["deleted"]
    with get_session_factory()() as session:
        assert session.get(ManagedUnit, "app-owned-service") is not None
        assert session.get(Deployment, "dep_app_protected") is not None
        assert session.get(ApplicationDeployment, "dep_app_protected") is not None
