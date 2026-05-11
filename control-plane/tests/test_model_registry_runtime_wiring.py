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
    mc = yaml.safe_load((root / "app/bootstrap/manifests/ai-engineer-model-config-service.yaml").read_text(encoding="utf-8"))
    ar = yaml.safe_load((root / "app/bootstrap/manifests/agent-runtime-service.yaml").read_text(encoding="utf-8"))
    expected = settings.platform_models_local_yaml_container_path
    env_mc = svc._build_runtime_env(UnitManifest.model_validate(mc), "svc-a")
    env_ar = svc._build_runtime_env(UnitManifest.model_validate(ar), "svc-b")
    assert env_mc.get("AI_ENGINEER_MODELS_CONFIG_PATH") == expected
    assert env_ar.get("AGENT_RUNTIME_MODELS_CONFIG_PATH") == expected


def test_model_registry_manifests_share_bind_mount_target() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("ai-engineer-model-config-service.yaml", "agent-runtime-service.yaml"):
        manifest = yaml.safe_load((root / "app/bootstrap/manifests" / name).read_text(encoding="utf-8"))
        targets = [m.get("target") for m in manifest.get("mounts", [])]
        assert "/app/shared-model-registry" in targets


def test_bootstrap_lists_model_config_before_gateway() -> None:
    from app.services.bootstrap_service import BOOTSTRAP_UNITS

    units = list(BOOTSTRAP_UNITS)
    assert units.index("ai-engineer-model-config-service") < units.index("platform-api-gateway")
