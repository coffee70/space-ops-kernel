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
    mc = yaml.safe_load((root / "app/bootstrap/manifests/model-registry-service.yaml").read_text(encoding="utf-8"))
    ar = yaml.safe_load((root / "app/bootstrap/manifests/agent-runtime-service.yaml").read_text(encoding="utf-8"))
    expected_path = settings.platform_models_local_yaml_container_path
    cp = settings.platform_control_plane_url.rstrip("/")

    env_mc = svc._build_runtime_env(UnitManifest.model_validate(mc), "svc-a")
    env_ar = svc._build_runtime_env(UnitManifest.model_validate(ar), "svc-b")

    assert env_mc.get("MODEL_CONFIG_PATH") == expected_path
    assert env_ar.get("MODEL_REGISTRY_BASE_URL") == f"{cp}/internal/runtime-services/model-registry-service"


def test_build_runtime_env_injects_persistent_vehicle_config_root_for_editor() -> None:
    from app.config import Settings
    from app.deployments.service import DeploymentService
    from app.schemas import UnitManifest

    settings = Settings(database_url="postgresql://u:p@localhost:5432/db", workspace_root=Path("/tmp/workspace-root"))
    svc = DeploymentService(settings, MagicMock(), MagicMock())
    root = Path(__file__).resolve().parents[1]
    vc = yaml.safe_load((root / "app/bootstrap/manifests/vehicle-config-service.yaml").read_text(encoding="utf-8"))

    env = svc._build_runtime_env(UnitManifest.model_validate(vc), "svc-vc")

    assert env["VEHICLE_CONFIG_ROOT"] == "/app/vehicle-configurations"


def test_model_registry_manifest_uses_named_volume_not_host_bind() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "app/bootstrap/manifests/model-registry-service.yaml").read_text(encoding="utf-8"))

    assert manifest.get("mounts", []) == []
    assert manifest.get("named_volumes") == [
        {"name": "model_registry_data", "target": "/app/model-registry", "read_only": False}
    ]


def test_vehicle_config_manifest_uses_named_volume() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "app/bootstrap/manifests/vehicle-config-service.yaml").read_text(encoding="utf-8"))

    assert manifest.get("named_volumes") == [
        {"name": "vehicle_config_data", "target": "/app/vehicle-configurations", "read_only": False}
    ]


def test_bootstrap_puts_model_registry_before_agent_runtime_then_gateway() -> None:
    from app.services.bootstrap_service import BOOTSTRAP_UNITS

    units = list(BOOTSTRAP_UNITS)
    assert units.index("model-registry-service") < units.index("agent-runtime-service")
    assert units.index("agent-runtime-service") < units.index("platform-api-gateway")


def test_model_registry_manifest_has_no_runtime_dependencies() -> None:
    root = Path(__file__).resolve().parents[1]
    mr = yaml.safe_load((root / "app/bootstrap/manifests/model-registry-service.yaml").read_text(encoding="utf-8"))
    assert mr.get("dependencies", []) == []


def test_gateway_dependencies_include_model_registry_service() -> None:
    root = Path(__file__).resolve().parents[1]
    gw = yaml.safe_load((root / "app/bootstrap/manifests/platform-api-gateway.yaml").read_text(encoding="utf-8"))
    assert "model-registry-service" in gw.get("dependencies", [])
