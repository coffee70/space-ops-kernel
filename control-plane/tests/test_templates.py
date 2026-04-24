from __future__ import annotations

from pathlib import Path

import yaml


def test_template_catalog_and_scaffold(client) -> None:
    templates_response = client.get("/templates")
    assert templates_response.status_code == 200
    template_ids = [item["template_id"] for item in templates_response.json()]
    assert template_ids == ["frontend-module", "node-service", "python-service"]

    scaffold_response = client.post(
        "/templates/frontend-module/scaffold",
        json={
            "branch": "main",
            "unit_id": "thermal-balance-module",
            "display_name": "Thermal Balance",
            "package_owner": "space-ops-apps",
            "discovery": {"route_slug": "thermal-balance", "description": "Thermal balance panel"},
        },
        headers={"X-Actor-Id": "operator", "X-Actor-Name": "Operator"},
    )
    assert scaffold_response.status_code == 200
    payload = scaffold_response.json()
    assert "manifests/units/thermal-balance-module.yaml" in payload["changed_files"]
    assert "project/space-ops-apps/modules/thermal-balance-module/server.js" in payload["changed_files"]

    workspace = Path(client.get("/health").json()["workspace_root"]).parent
    manifest = yaml.safe_load((workspace / "manifests/units/thermal-balance-module.yaml").read_text(encoding="utf-8"))
    assert manifest["runtime_template"] == "frontend-module"
    assert manifest["discovery"]["route_slug"] == "thermal-balance"


def test_duplicate_unit_id_rejected(client) -> None:
    response = client.post(
        "/templates/python-service/scaffold",
        json={
            "branch": "main",
            "unit_id": "derived-telemetry-service",
            "display_name": "Duplicate",
            "package_owner": "space-ops-platform",
        },
    )
    assert response.status_code == 400
    assert "already exists" in response.json()["detail"]


def test_template_request_does_not_reimport_seed_source(client, control_plane_env) -> None:
    file_path = "project/space-ops-apps/README.md"

    write_response = client.put(
        "/code/file",
        json={
            "branch": "main",
            "path": file_path,
            "content": "managed apps edit\n",
        },
    )
    assert write_response.status_code == 200

    commit_response = client.post(
        "/code/commits",
        json={"branch": "main", "message": "Edit apps managed fork"},
        headers={"X-Actor-Id": "operator", "X-Actor-Name": "Operator"},
    )
    assert commit_response.status_code == 200

    mounted_seed_file = control_plane_env / "space-ops-apps" / "README.md"
    mounted_seed_file.write_text("mounted apps seed changed\n", encoding="utf-8")

    templates_response = client.get("/templates")
    assert templates_response.status_code == 200

    file_response = client.get(
        "/code/file",
        params={"branch": "main", "path": file_path},
    )
    assert file_response.status_code == 200
    assert file_response.json()["data"]["content"] == "managed apps edit\n"
