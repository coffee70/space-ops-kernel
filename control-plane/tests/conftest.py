from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def control_plane_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace_root = tmp_path / "workspace"
    kernel_root = workspace_root / "space-ops-kernel"
    platform_root = workspace_root / "space-ops-platform"
    apps_root = workspace_root / "space-ops-apps"
    runtime_root = kernel_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)

    _write(platform_root / "README.md", "platform")
    _write(apps_root / "README.md", "apps")
    _write(
        platform_root / "backend/services/derived-telemetry-service/requirements.txt",
        "fastapi>=0.109\nuvicorn[standard]>=0.27\n",
    )
    _write(
        platform_root / "backend/services/derived-telemetry-service/Dockerfile",
        "FROM python:3.11-slim\nWORKDIR /app\nCOPY . /app\nRUN pip install -r requirements.txt\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8080\"]\n",
    )
    _write(
        platform_root / "backend/services/derived-telemetry-service/app/main.py",
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/health')\ndef health():\n    return {'status': 'ok'}\n",
    )
    _write(
        apps_root / "modules/battery-efficiency-module/Dockerfile",
        "FROM node:20-alpine\nWORKDIR /app\nCOPY . /app\nCMD [\"node\", \"server.js\"]\n",
    )
    _write(
        apps_root / "modules/battery-efficiency-module/server.js",
        "require('http').createServer((req,res)=>{if(req.url==='/health'){res.end('{\"status\":\"ok\"}');return;}res.end('ok');}).listen(process.env.PORT||3100,'0.0.0.0');\n",
    )

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("PLATFORM_SOURCE_ROOT", str(platform_root))
    monkeypatch.setenv("APPS_SOURCE_ROOT", str(apps_root))
    monkeypatch.setenv("RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(runtime_root / 'test.db').as_posix()}")
    monkeypatch.setenv("RUNTIME_STRATEGY", "stub")

    return workspace_root


@pytest.fixture
def client(control_plane_env: Path) -> TestClient:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    import app.config
    import app.db
    import app.migrations
    import app.main

    app.config.get_settings.cache_clear()
    app.db._engine = None
    app.db._SessionLocal = None
    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.migrations)
    importlib.reload(app.main)
    app.migrations.run_migrations(app.db.get_engine())

    with TestClient(app.main.app) as test_client:
        yield test_client
