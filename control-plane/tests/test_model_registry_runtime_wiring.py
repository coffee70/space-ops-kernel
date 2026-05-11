"""Model registry shared mount and control-plane env wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import yaml


def test_build_runtime_env_injects_model_registry_paths() -> None:
    from app.config import Settings
    from app.deployments.service import DeploymentService
    from app.schemas import UnitManifest

    settings = Settings(database_url="postgresql://u:p@localhost:5432/db", workspace_root=Path("/tmp/workspace-root"))
    svc = DeploymentService(settings, MagicMock(), MagicMock())
    root = Path(__file__).resolve().parents[1]
    mc = yaml.safe_load((root / "app/bootstrap/manifests/model-config-service.yaml").read_text(encoding="utf-8"))
    ar = yaml.safe_load((root / "app/bootstrap/manifests/agent-runtime-service.yaml").read_text(encoding="utf-8"))
    expected_path = settings.platform_models_local_yaml_container_path
    cp = settings.platform_control_plane_url.rstrip("/")

    env_mc = svc._build_runtime_env(UnitManifest.model_validate(mc), "svc-a")
    env_ar = svc._build_runtime_env(UnitManifest.model_validate(ar), "svc-b")

    assert env_mc.get("MODEL_CONFIG_PATH") == expected_path
    assert env_mc.get("AGENT_RUNTIME_BASE_URL") == f"{cp}/internal/runtime-services/agent-runtime-service"
    assert env_ar.get("AGENT_RUNTIME_MODELS_CONFIG_PATH") == expected_path


def test_model_registry_manifests_share_bind_mount_target() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("model-config-service.yaml", "agent-runtime-service.yaml"):
        manifest = yaml.safe_load((root / "app/bootstrap/manifests" / name).read_text(encoding="utf-8"))
        targets = [m.get("target") for m in manifest.get("mounts", [])]
        assert "/app/shared-model-registry" in targets


def test_bootstrap_puts_agent_runtime_before_model_config_then_gateway() -> None:
    from app.services.bootstrap_service import BOOTSTRAP_UNITS

    units = list(BOOTSTRAP_UNITS)
    assert units.index("agent-runtime-service") < units.index("model-config-service")
    assert units.index("model-config-service") < units.index("platform-api-gateway")


def test_model_config_manifest_depends_on_agent_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    mc = yaml.safe_load((root / "app/bootstrap/manifests/model-config-service.yaml").read_text(encoding="utf-8"))
    assert "agent-runtime-service" in mc.get("discovery", {}).get("depends_on", [])


def test_gateway_discovery_depends_on_model_config_service() -> None:
    root = Path(__file__).resolve().parents[1]
    gw = yaml.safe_load((root / "app/bootstrap/manifests/platform-api-gateway.yaml").read_text(encoding="utf-8"))
    assert "model-config-service" in gw.get("discovery", {}).get("depends_on", [])
