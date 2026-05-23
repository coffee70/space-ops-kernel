from __future__ import annotations

from pathlib import Path

import yaml


def _compose_payload() -> dict:
    compose_file = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    return yaml.safe_load(compose_file.read_text(encoding="utf-8"))


def test_default_compose_stack_does_not_include_static_mission_control_ui() -> None:
    services = _compose_payload()["services"]

    assert "mission-control-ui" not in services
    assert all(
        service.get("container_name") != "telemetry-mission-control-ui"
        for service in services.values()
        if isinstance(service, dict)
    )


def test_platform_edge_proxy_no_longer_depends_on_static_frontend() -> None:
    services = _compose_payload()["services"]
    depends_on = services["platform-edge-proxy"].get("depends_on", {})

    assert "mission-control-ui" not in depends_on
    assert set(depends_on) == {"platform-api", "control-plane"}
