from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.config import Settings
from app.services.system_status import ContainerStatus, SystemStatusService


def _settings(tmp_path: Path, *, runtime_strategy: str = "stub") -> Settings:
    workspace = tmp_path / "workspace"
    kernel = workspace / "space-ops-kernel"
    kernel.mkdir(parents=True)
    return Settings(
        database_url="postgresql://u:p@localhost:5432/db",
        workspace_root=workspace,
        runtime_strategy=runtime_strategy,
    )


def _service(settings: Settings) -> SystemStatusService:
    return SystemStatusService(settings, None)  # type: ignore[arg-type]


def _unit(unit_id: str, **overrides: Any) -> SimpleNamespace:
    values = {
        "unit_id": unit_id,
        "display_name": unit_id.replace("-", " ").title(),
        "active_deployment_id": None,
        "deployment_status": "pending",
        "health_status": "unknown",
        "runtime_kind": "service",
        "runtime_template": "python-service",
        "updated_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _deployment(deployment_id: str, unit_id: str, **overrides: Any) -> SimpleNamespace:
    values = {
        "deployment_id": deployment_id,
        "unit_id": unit_id,
        "branch": "main",
        "commit_sha": "abc123",
        "status": "healthy",
        "health_status": "passing",
        "failure_reason": None,
        "runtime_ref": {"service_name": unit_id},
        "health_checked_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeRegistry:
    def __init__(self, units: list[SimpleNamespace] | None = None, deployments: dict[str, SimpleNamespace] | None = None):
        self.units = units or []
        self.deployments = deployments or {}

    def get_units(self):
        return self.units

    def get_deployment(self, deployment_id: str):
        return self.deployments.get(deployment_id)

    def get_latest_deployment_for_unit(self, unit_id: str):
        matches = [deployment for deployment in self.deployments.values() if deployment.unit_id == unit_id]
        return matches[-1] if matches else None

    def get_events(self, deployment_id: str):
        return []

    def read_logs(self, logs_root: Path, deployment_id: str):
        return ""


def test_expected_core_services_come_from_compose_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.resolved_compose_file.write_text(
        """
services:
  postgres:
    image: postgres
  control-plane:
    build: .
""",
        encoding="utf-8",
    )

    assert _service(settings).expected_core_services() == ["postgres", "control-plane"]


def test_core_services_normalize_missing_healthy_and_crashed(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    service.expected_core_services = lambda: ["postgres", "control-plane", "nats"]  # type: ignore[method-assign]

    summary = service._build_core_summary(
        {
            "postgres": ContainerStatus(service="postgres", state="running", status="Up 1 minute", health="healthy"),
            "control-plane": ContainerStatus(service="control-plane", state="exited", status="Exited (1)", exit_code=1),
        }
    )

    states = {item.id: item.ui_state for item in summary.services}
    assert summary.expected_count == 3
    assert summary.existing_count == 2
    assert states == {"postgres": "healthy", "control-plane": "crashed", "nats": "missing"}


def test_runtime_summary_uses_bootstrap_units_and_marks_missing(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    service.registry = FakeRegistry()  # type: ignore[assignment]

    summary = service._build_runtime_summary({}, False, {"units": []})

    assert summary.expected_count == 21
    assert summary.existing_count == 0
    assert summary.missing_count == 21
    assert summary.services[0].id == "vehicle-config-service"
    assert summary.services[0].ui_state == "missing"


def test_runtime_healthy_requires_registry_and_container_in_docker_mode(tmp_path: Path) -> None:
    settings = _settings(tmp_path, runtime_strategy="docker")
    service = _service(settings)
    deployment = _deployment("dep_1", "vehicle-config-service")
    service.registry = FakeRegistry(
        units=[_unit("vehicle-config-service", active_deployment_id="dep_1", deployment_status="healthy", health_status="passing")],
        deployments={"dep_1": deployment},
    )  # type: ignore[assignment]

    healthy = service._build_runtime_summary(
        {"vehicle-config-service": ContainerStatus(service="vehicle-config-service", state="running", health="healthy")},
        True,
        {"units": []},
    ).services[0]
    stale = service._build_runtime_summary({}, True, {"units": []}).services[0]

    assert healthy.ui_state == "healthy"
    assert healthy.exists is True
    assert stale.ui_state == "stale"
    assert stale.exists is False


def test_runtime_failed_includes_failure_context_and_logs_url(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    deployment = _deployment(
        "dep_failed",
        "vehicle-config-service",
        status="failed",
        health_status="failing",
        failure_reason="build failed",
    )
    service.registry = FakeRegistry(
        units=[_unit("vehicle-config-service")],
        deployments={"dep_failed": deployment},
    )  # type: ignore[assignment]

    row = service._build_runtime_summary({}, False, {"units": []}).services[0]

    assert row.ui_state == "failed"
    assert row.failure_reason == "build failed"
    assert row.latest_error == "build failed"
    assert row.logs_url == "/deployments/dep_failed/logs"


def test_bootstrap_status_is_merged_onto_runtime_rows(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    service.registry = FakeRegistry()  # type: ignore[assignment]

    row = service._build_runtime_summary(
        {},
        False,
        {"units": [{"unit_id": "vehicle-config-service", "status": "deploying"}]},
    ).services[0]

    assert row.bootstrap_status == "deploying"
    assert row.ui_state == "deploying"


def test_overall_state_prioritizes_broken_then_degraded_then_healthy(tmp_path: Path) -> None:
    service = _service(_settings(tmp_path))
    service.registry = FakeRegistry()  # type: ignore[assignment]
    service.expected_core_services = lambda: ["postgres"]  # type: ignore[method-assign]
    healthy_core = service._build_core_summary({"postgres": ContainerStatus(service="postgres", state="running", health="healthy")})
    broken_core = service._build_core_summary({})
    runtime_healthy = service._summarize([])
    runtime_degraded = service._summarize([
        service._build_runtime_summary({}, False, {"units": [{"unit_id": "vehicle-config-service", "status": "deploying"}]}).services[0]
    ])

    assert service._overall_state(broken_core, runtime_healthy, {"status": "completed"}) == "broken"
    assert service._overall_state(healthy_core, runtime_degraded, {"status": "running"}) == "degraded"
    assert service._overall_state(healthy_core, runtime_healthy, {"status": "completed"}) == "healthy"


def test_container_inspector_merges_compose_and_project_label_output(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.resolved_compose_file.write_text("services:\n  postgres:\n    image: postgres\n", encoding="utf-8")
    service = _service(settings)

    def fake_run(command, **_kwargs):
        if command[:2] == ["docker", "compose"]:
            return SimpleNamespace(stdout='{"Service":"postgres","State":"running","Health":"healthy"}\n')
        return SimpleNamespace(
            stdout=(
                '{"Names":"runtime-1","State":"running",'
                '"Labels":"com.docker.compose.project=space-ops-kernel,com.docker.compose.service=vehicle-config-service-dep-1"}\n'
            )
        )

    monkeypatch.setattr("app.services.system_status.subprocess.run", fake_run)

    containers, docker_available = service.inspect_compose_containers()

    assert docker_available is True
    assert containers["postgres"].health == "healthy"
    assert containers["vehicle-config-service-dep-1"].state == "running"
