from __future__ import annotations

from pathlib import Path

import yaml


def test_satnogs_adapter_manifest_declares_adapter_capabilities() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "bootstrap"
        / "manifests"
        / "satnogs-adapter-service.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    assert manifest["unit_id"] == "satnogs-adapter-service"
    assert manifest["runtime_template"] == "python-service"
    assert manifest["discovery"]["service_slug"] == "satnogs-adapter-service"
    assert {"adapter", "satnogs", "ax25", "aprs", "observation-sync", "realtime-ingest"} <= set(
        manifest["discovery"]["capability_tags"]
    )
    assert {"source-registry-service", "telemetry-ingest-service"} <= set(manifest["dependencies"])
    assert "depends_on" not in manifest["discovery"]


def test_simulator_manifests_declare_provider_capabilities() -> None:
    manifest_root = Path(__file__).resolve().parents[1] / "app" / "bootstrap" / "manifests"

    for unit_id in ("simulator-service", "simulator-2-service"):
        manifest = yaml.safe_load((manifest_root / f"{unit_id}.yaml").read_text(encoding="utf-8"))

        assert manifest["unit_id"] == unit_id
        assert manifest["runtime_template"] == "python-service"
        assert manifest["discovery"]["service_slug"] == unit_id
        assert manifest["health"]["path"] == "/health"
        assert {
            "simulator",
            "telemetry-provider",
            "telemetry-ingest",
            "scenario-streaming",
            "realtime",
        } <= set(manifest["discovery"]["capability_tags"])
        assert {"source-registry-service", "telemetry-ingest-service"} <= set(manifest["dependencies"])
        assert "depends_on" not in manifest["discovery"]
