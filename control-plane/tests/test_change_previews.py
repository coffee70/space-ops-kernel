"""Tests for the chat-native change preview deploy/revert adapter."""

from __future__ import annotations


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
    assert payload["status"] == "healthy"
    assert payload["registered"] is True
    assert payload["unit_id"] == "derived-telemetry-service"
    assert payload["target_unit_id"] == "derived-telemetry-service"
    assert payload["target_application_id"] == "telemetry"
    assert payload["conversation_id"] == "conv-deploy-1"
    assert payload["agent_run_id"] == "run-deploy-1"
    assert payload["branch"] == "main"
    assert payload["commit_sha"]
    assert payload["logs_url"].startswith("/deployments/")


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
    assert revert_payload["status"] == "healthy"
    assert revert_payload["branch"] == "main"
    assert revert_payload["preview_deployment_id"] == preview_deployment_id
    assert revert_payload["target_unit_id"] == "derived-telemetry-service"
    assert revert_payload["target_application_id"] == "telemetry"

    poll = client.get(f"/deployments/{revert_payload['deployment_id']}")
    assert poll.status_code == 200
    poll_payload = poll.json()
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
