from __future__ import annotations


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
        session.add(
            ManagedUnit(
                unit_id="deleteable-service",
                display_name="Deleteable Service",
                package_owner="space-ops-platform",
                runtime_kind="service",
                runtime_template="python-service",
                source_path="project/space-ops-platform/backend/services/deleteable-service",
                deployment_status="healthy",
                health_status="passing",
                discovery_metadata_json={},
                active_deployment_id=deployment_id,
                delete_eligible=True,
            )
        )
        session.flush()
        session.add(
            Deployment(
                deployment_id=deployment_id,
                unit_id="deleteable-service",
                branch="main",
                commit_sha="abc1234",
                status="healthy",
                health_status="passing",
                artifact_ref=str(runtime_root / "artifacts" / deployment_id),
                runtime_ref={
                    "service_name": "deleteable-service-dep-deleteable",
                    "compose_file": str(runtime_root / "generated" / "compose" / f"{deployment_id}.yaml"),
                    "env_file": str(runtime_root / "generated" / "env" / f"{deployment_id}.env"),
                    "transport": {"scheme": "http", "host": "deleteable-service-dep-deleteable", "port": 8080},
                    "health": {"path": "/health"},
                    "proxy": {"base_path": ""},
                },
                delete_eligible=True,
            )
        )
        session.add(DeploymentEvent(deployment_id=deployment_id, event_type="healthy", message="ok"))
        session.add(UnitHealthSnapshot(unit_id="deleteable-service", deployment_id=deployment_id, status="passing"))
        session.commit()

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
