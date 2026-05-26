"""Tests for the chat-native change preview deploy/revert adapter."""

from __future__ import annotations


def _execute_queued_deployment(client, deployment_id: str) -> dict:
    from app.config import get_settings
    from app.deployments.worker import DeploymentWorker

    assert DeploymentWorker(get_settings()).run_once() == deployment_id
    response = client.get(f"/deployments/{deployment_id}")
    assert response.status_code == 200
    return response.json()


def test_change_preview_deploy_routes_through_deployment_service(client) -> None:
    response = client.post(
        "/change-previews/deploy",
        json={
            "branch": "main",
            "target_unit_id": "derived-telemetry-service",
            "target_application_id": "telemetry",
            "conversation_id": "conv-deploy-1",
            "agent_run_id": "run-deploy-1",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["registered"] is False
    assert payload["unit_id"] == "derived-telemetry-service"
    assert payload["target_unit_id"] == "derived-telemetry-service"
    assert payload["target_application_id"] == "telemetry"
    assert payload["conversation_id"] == "conv-deploy-1"
    assert payload["agent_run_id"] == "run-deploy-1"
    assert payload["branch"] == "main"
    assert payload["deployment_intent"] == "deploy_preview"
    assert payload["commit_sha"]
    assert payload["logs_url"].startswith("/deployments/")
    executed = _execute_queued_deployment(client, payload["deployment_id"])
    assert executed["status"] == "healthy"
    assert executed["registered"] is True


def test_change_preview_revert_submits_baseline_deployment(client) -> None:
    preview_branch = "preview/telemetry-revert-test"

    branch_response = client.post(
        "/code/branches",
        json={"branch": preview_branch, "from_branch": "main"},
    )
    assert branch_response.status_code == 200

    file_response = client.put(
        "/code/file",
        json={
            "branch": preview_branch,
            "path": "project/space-ops-platform/backend/services/derived-telemetry-service/app/main.py",
            "content": (
                "from fastapi import FastAPI\n"
                "app = FastAPI()\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {'status': 'preview'}\n"
            ),
        },
    )
    assert file_response.status_code == 200

    commit_response = client.post(
        "/code/commits",
        json={"branch": preview_branch, "message": "Preview change"},
    )
    assert commit_response.status_code == 200

    preview_deploy = client.post(
        "/change-previews/deploy",
        json={
            "branch": preview_branch,
            "target_unit_id": "derived-telemetry-service",
            "target_application_id": "telemetry",
            "conversation_id": "conv-revert-1",
            "agent_run_id": "run-revert-1",
        },
    )
    assert preview_deploy.status_code == 200
    preview_payload = preview_deploy.json()
    preview_deployment_id = preview_payload["deployment_id"]
    assert preview_payload["branch"] == preview_branch
    _execute_queued_deployment(client, preview_deployment_id)

    revert_response = client.post(
        "/change-previews/revert",
        json={
            "target_unit_id": "derived-telemetry-service",
            "target_application_id": "telemetry",
            "baseline_branch": "main",
            "preview_deployment_id": preview_deployment_id,
            "conversation_id": "conv-revert-1",
            "agent_run_id": "run-revert-1",
        },
    )
    assert revert_response.status_code == 200, revert_response.text
    revert_payload = revert_response.json()
    assert revert_payload["status"] == "queued"
    assert revert_payload["branch"] == "main"
    assert revert_payload["deployment_intent"] == "revert_to_baseline"
    assert revert_payload["preview_deployment_id"] == preview_deployment_id
    assert revert_payload["target_unit_id"] == "derived-telemetry-service"
    assert revert_payload["target_application_id"] == "telemetry"

    poll = client.get(f"/deployments/{revert_payload['deployment_id']}")
    assert poll.status_code == 200
    poll_payload = poll.json()
    assert poll_payload["status"] == "queued"
    poll_payload = _execute_queued_deployment(client, revert_payload["deployment_id"])
    assert poll_payload["status"] == "healthy"
    assert poll_payload["health_status"] == "passing"


def test_change_preview_revert_rejects_unknown_preview_deployment(client) -> None:
    response = client.post(
        "/change-previews/revert",
        json={
            "target_unit_id": "derived-telemetry-service",
            "baseline_branch": "main",
            "preview_deployment_id": "dep_unknown",
        },
    )
    assert response.status_code == 404


def test_change_preview_revert_rejects_preview_deployment_for_other_unit(client) -> None:
    """Preview deployment unit must match the requested target unit (HTTP 400, not 404)."""

    preview_deploy = client.post(
        "/change-previews/deploy",
        json={
            "branch": "main",
            "target_unit_id": "derived-telemetry-service",
            "target_application_id": "telemetry",
            "agent_run_id": "run-mismatch-1",
        },
    )
    assert preview_deploy.status_code == 200, preview_deploy.text
    preview_deployment_id = preview_deploy.json()["deployment_id"]

    response = client.post(
        "/change-previews/revert",
        json={
            # Pretend that the same preview deployment belongs to a different unit.
            "target_unit_id": "telemetry-query-service",
            "baseline_branch": "main",
            "preview_deployment_id": preview_deployment_id,
        },
    )
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "preview_deployment_unit_mismatch"
    assert detail["preview_unit_id"] == "derived-telemetry-service"
    assert detail["requested_target_unit_id"] == "telemetry-query-service"


def test_change_preview_revert_uses_baseline_commit_sha_when_provided(client) -> None:
    """Revert must restore the exact baseline commit SHA captured at preview time."""

    initial_root = client.get("/code/roots")
    assert initial_root.status_code == 200
    baseline_commit_sha = initial_root.json()["commit_sha"]
    assert baseline_commit_sha

    # Create a preview branch and advance it: this proves the revert ignores
    # the current branch head and uses the baseline_commit_sha that was
    # captured when the preview was created.
    branch_response = client.post(
        "/code/branches",
        json={"branch": "preview/baseline-commit-test", "from_branch": "main"},
    )
    assert branch_response.status_code == 200
    file_response = client.put(
        "/code/file",
        json={
            "branch": "preview/baseline-commit-test",
            "path": "project/space-ops-platform/backend/services/derived-telemetry-service/app/main.py",
            "content": "from fastapi import FastAPI\napp = FastAPI()\n",
        },
    )
    assert file_response.status_code == 200
    commit_response = client.post(
        "/code/commits",
        json={"branch": "preview/baseline-commit-test", "message": "Preview change"},
    )
    assert commit_response.status_code == 200
    advance_commit_sha = commit_response.json()["commit_sha"]
    assert advance_commit_sha != baseline_commit_sha

    revert_response = client.post(
        "/change-previews/revert",
        json={
            "target_unit_id": "derived-telemetry-service",
            "baseline_branch": "main",
            "baseline_commit_sha": baseline_commit_sha,
        },
    )
    assert revert_response.status_code == 200, revert_response.text
    revert_payload = revert_response.json()
    assert revert_payload["status"] == "queued"
    # The revert deployment must record the requested baseline commit, not the
    # latest commit on the preview branch.
    assert revert_payload["commit_sha"] == baseline_commit_sha
    assert revert_payload["commit_sha"] != advance_commit_sha


def test_create_branch_returns_base_branch_and_base_commit_sha(client) -> None:
    """Branch creation must expose base metadata so the agent can build change.summary."""

    initial_root = client.get("/code/roots")
    assert initial_root.status_code == 200
    expected_base_commit_sha = initial_root.json()["commit_sha"]

    branch_response = client.post(
        "/code/branches",
        json={"branch": "preview/base-metadata-check", "from_branch": "main"},
    )
    assert branch_response.status_code == 200, branch_response.text
    payload = branch_response.json()
    data = payload.get("data", {})
    assert data.get("base_branch") == "main"
    assert data.get("base_commit_sha") == expected_base_commit_sha
    assert payload.get("commit_sha") == expected_base_commit_sha
