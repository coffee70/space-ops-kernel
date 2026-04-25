from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_test_database(base_url: str) -> tuple[str, callable]:
    try:
        import psycopg2
        from psycopg2 import sql
    except ImportError as exc:  # pragma: no cover - exercised only when dependency is missing
        pytest.skip(f"Postgres test dependency unavailable: {exc}")

    url = make_url(base_url)
    db_name = f"{(url.database or 'control_plane_test').replace('-', '_')}_{uuid.uuid4().hex[:8]}"

    try:
        admin_connection = psycopg2.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname="postgres",
        )
    except Exception as exc:  # pragma: no cover - depends on local services
        pytest.skip(f"Postgres test database unavailable: {exc}")

    admin_connection.autocommit = True
    with admin_connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    admin_connection.close()

    def drop_database() -> None:
        cleanup_connection = psycopg2.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname="postgres",
        )
        cleanup_connection.autocommit = True
        with cleanup_connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (db_name,),
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(db_name)))
        cleanup_connection.close()

    return url.set(database=db_name).render_as_string(hide_password=False), drop_database


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
        apps_root / "applications/embedded-demo-application/Dockerfile",
        "FROM node:20-alpine\nWORKDIR /app\nCOPY . /app\nCMD [\"node\", \"server.js\"]\n",
    )
    _write(
        apps_root / "applications/embedded-demo-application/server.js",
        "const basePath=(process.env.APPLICATION_PROXY_BASE_PATH||'/').replace(/\\/+$/,'')||'/';"
        "require('http').createServer((req,res)=>{if(req.url==='/health'){res.end('{\"status\":\"ok\"}');return;}"
        "const requestPath=req.url.split('?')[0];"
        "if(basePath!=='/'&&requestPath!==basePath&&!requestPath.startsWith(`${basePath}/`)){res.statusCode=404;res.end('not found');return;}"
        "res.end('ok');}).listen(process.env.PORT||3100,'0.0.0.0');\n",
    )
    _write(
        apps_root / "mission-control-ui/src/applications/overview/application.seed.json",
        (
            '{"applicationId":"overview","title":"Overview","description":"Mission overview dashboard.",'
            '"iconKey":"layout-dashboard","iconColor":"#38bdf8","iconBackground":"rgba(56, 189, 248, 0.16)",'
            '"applicationType":"native","routePath":"/apps/overview","loaderKey":"overview","version":"0.1.0",'
            '"enabled":true,"sortOrder":10,"owner":"space-ops-apps","capabilities":["telemetry-overview"]}'
        ),
    )
    _write(
        apps_root / "mission-control-ui/src/applications/telemetry/application.seed.json",
        (
            '{"applicationId":"telemetry","title":"Telemetry","description":"Telemetry inventory and detail views.",'
            '"iconKey":"chart-no-axes-combined","iconColor":"#34d399","iconBackground":"rgba(52, 211, 153, 0.16)",'
            '"applicationType":"native","routePath":"/apps/telemetry","loaderKey":"telemetry","version":"0.1.0",'
            '"enabled":true,"sortOrder":20,"owner":"space-ops-apps","capabilities":["telemetry-analysis"]}'
        ),
    )
    _write(
        apps_root / "mission-control-ui/src/applications/planning/application.seed.json",
        (
            '{"applicationId":"planning","title":"Planning","description":"Mission planning and orbit views.",'
            '"iconKey":"satellite-dish","iconColor":"#f59e0b","iconBackground":"rgba(245, 158, 11, 0.16)",'
            '"applicationType":"native","routePath":"/apps/planning","loaderKey":"planning","version":"0.1.0",'
            '"enabled":true,"sortOrder":30,"owner":"space-ops-apps","capabilities":["mission-planning"]}'
        ),
    )
    _write(
        apps_root / "mission-control-ui/src/applications/sources/application.seed.json",
        (
            '{"applicationId":"sources","title":"Sources","description":"Source registry and configuration tools.",'
            '"iconKey":"server","iconColor":"#fb7185","iconBackground":"rgba(251, 113, 133, 0.16)",'
            '"applicationType":"native","routePath":"/apps/sources","loaderKey":"sources","version":"0.1.0",'
            '"enabled":true,"sortOrder":40,"owner":"space-ops-apps","capabilities":["source-management"]}'
        ),
    )
    _write(
        apps_root / "mission-control-ui/src/applications/workspace/application.seed.json",
        (
            '{"applicationId":"workspace","title":"Workspace","description":"Open VS Code Server workspace for platform files and tools.",'
            '"iconKey":"folder-code","iconColor":"#38bdf8","iconBackground":"rgba(56, 189, 248, 0.16)",'
            '"applicationType":"embedded","routePath":"/apps/workspace","embeddedUrl":"/workspace","proxyBasePath":"/workspace",'
            '"version":"0.1.0","enabled":true,"iframeSandbox":"allow-scripts allow-same-origin allow-forms","iframeAllow":"",'
            '"sortOrder":50,"owner":"space-ops-apps","capabilities":["development-workspace"]}'
        ),
    )
    _write(
        apps_root / "mission-control-ui/src/applications/battery-efficiency/application.seed.json",
        (
            '{"applicationId":"battery-efficiency","title":"Battery Efficiency",'
            '"description":"Example native analysis app built inside the platform shell over Layer 2 telemetry.",'
            '"iconKey":"battery","iconColor":"#f59e0b","iconBackground":"rgba(245, 158, 11, 0.16)",'
            '"applicationType":"native","routePath":"/apps/battery-efficiency","loaderKey":"battery-efficiency",'
            '"version":"0.1.0","enabled":true,"sortOrder":60,"owner":"space-ops-apps",'
            '"capabilities":["telemetry-analysis","battery-analysis"]}'
        ),
    )

    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("PLATFORM_SOURCE_ROOT", str(platform_root))
    monkeypatch.setenv("APPS_SOURCE_ROOT", str(apps_root))
    monkeypatch.setenv("RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("RUNTIME_STRATEGY", "stub")
    base_database_url = (
        os.environ.get("KERNEL_TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or "postgresql://telemetry:telemetry@localhost:5432/control_plane_test"
    )
    test_database_url, cleanup_database = _create_test_database(base_database_url)
    monkeypatch.setenv("DATABASE_URL", test_database_url)

    try:
        yield workspace_root
    finally:
        cleanup_database()


@pytest.fixture
def client(control_plane_env: Path) -> TestClient:
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
