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
