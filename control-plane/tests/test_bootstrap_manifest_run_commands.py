from __future__ import annotations

from pathlib import Path

import yaml


def test_agent_runtime_bootstrap_manifest_run_command() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "app" / "bootstrap" / "manifests" / "agent-runtime-service.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["run"]["command"] == "node dist/server.js"
