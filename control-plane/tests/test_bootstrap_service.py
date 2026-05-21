from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace


def _manifest(*dependencies: str) -> SimpleNamespace:
    return SimpleNamespace(dependencies=list(dependencies))


def test_managed_fork_bootstrap_is_non_destructive_after_initial_import(control_plane_env: Path) -> None:
    import app.config
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)

    bootstrapper.ensure_bootstrapped()

    managed_file = settings.main_worktree_dir / "project/space-ops-platform/README.md"
    managed_file.write_text("managed-only change\n", encoding="utf-8")

    mounted_seed_file = control_plane_env / "space-ops-platform" / "README.md"
    mounted_seed_file.write_text("seed changed after bootstrap\n", encoding="utf-8")

    bootstrapper.ensure_bootstrapped()

    assert managed_file.read_text(encoding="utf-8") == "managed-only change\n"


def test_bootstrap_manifest_sync_updates_unchanged_seeded_manifest(control_plane_env: Path, monkeypatch) -> None:
    import app.config
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)
    seed_root = control_plane_env / "kernel-seeds"
    seed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrapper, "_seed_manifests_root", lambda: seed_root)

    seed_path = seed_root / "derived-telemetry-service.yaml"
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 1\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    managed_manifest = settings.main_worktree_dir / "manifests/units/derived-telemetry-service.yaml"
    assert managed_manifest.read_text(encoding="utf-8") == "unit_id: derived-telemetry-service\nversion: 1\n"

    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 2\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    assert managed_manifest.read_text(encoding="utf-8") == "unit_id: derived-telemetry-service\nversion: 2\n"
    state = json.loads(settings.bootstrap_seed_state_path.read_text(encoding="utf-8"))
    assert "manifests/units/derived-telemetry-service.yaml" in state


def test_bootstrap_manifest_sync_preserves_locally_edited_manifest(control_plane_env: Path, monkeypatch) -> None:
    import app.config
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)
    seed_root = control_plane_env / "kernel-seeds"
    seed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrapper, "_seed_manifests_root", lambda: seed_root)

    seed_path = seed_root / "derived-telemetry-service.yaml"
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 1\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    managed_manifest = settings.main_worktree_dir / "manifests/units/derived-telemetry-service.yaml"
    managed_manifest.write_text("unit_id: derived-telemetry-service\nversion: local-edit\n", encoding="utf-8")
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 2\n", encoding="utf-8")

    bootstrapper.ensure_bootstrapped()

    assert managed_manifest.read_text(encoding="utf-8") == "unit_id: derived-telemetry-service\nversion: local-edit\n"


def test_bootstrap_manifest_sync_creates_commit_when_seed_changes(control_plane_env: Path, monkeypatch) -> None:
    import app.config
    from app.git.repository import ManagedGitRepository
    from app.services.bootstrap_service import ManagedForkBootstrapper

    app.config.get_settings.cache_clear()
    importlib.reload(app.config)

    settings = app.config.get_settings()
    bootstrapper = ManagedForkBootstrapper(settings)
    seed_root = control_plane_env / "kernel-seeds"
    seed_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bootstrapper, "_seed_manifests_root", lambda: seed_root)

    seed_path = seed_root / "derived-telemetry-service.yaml"
    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 1\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    repository = ManagedGitRepository(settings)
    first_commit = repository.get_head_commit("main")

    seed_path.write_text("unit_id: derived-telemetry-service\nversion: 2\n", encoding="utf-8")
    bootstrapper.ensure_bootstrapped()

    second_commit = repository.get_head_commit("main")
    history = repository.get_history("main", "manifests/units/derived-telemetry-service.yaml", limit=1)

    assert second_commit != first_commit
    assert history[0]["subject"] == "Sync bootstrap manifests from control plane"


def test_runtime_bootstrapper_units_match_seed_manifest_inventory() -> None:
    from app.services.bootstrap_service import BOOTSTRAP_UNITS, REGISTRY_SEED_UNITS

    manifest_root = Path(__file__).resolve().parents[1] / "app" / "bootstrap" / "manifests"
    manifest_unit_ids = tuple(sorted(path.stem for path in manifest_root.glob("*.yaml")))

    assert tuple(sorted(REGISTRY_SEED_UNITS)) == manifest_unit_ids
    assert "mission-control-frontend-shell" in REGISTRY_SEED_UNITS
    assert "mission-control-frontend-shell" not in BOOTSTRAP_UNITS


def test_runtime_bootstrapper_loads_manifests_from_managed_worktree_only(tmp_path: Path, monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    main_worktree = tmp_path / "managed-fork" / "worktrees" / "main"
    manifest_root = main_worktree / "manifests" / "units"
    manifest_root.mkdir(parents=True)
    (manifest_root / "only-unit.yaml").write_text(
        """
unit_id: only-unit
display_name: Only Unit
package_owner: space-ops-platform
runtime_kind: service
runtime_template: python-service
source_path: project/space-ops-platform
build:
  command: echo build
run:
  command: echo run
health:
  type: http
  path: /health
  port: 8080
dependencies: []
discovery: {}
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("only-unit",))
    bootstrapper = RuntimeBootstrapper(SimpleNamespace(main_worktree_dir=main_worktree))

    manifests = bootstrapper._load_bootstrap_manifests()

    assert manifests["only-unit"].unit_id == "only-unit"
    assert manifests["only-unit"].dependencies == []


def test_runtime_bootstrapper_continues_after_failed_unit(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    attempted: list[str] = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            return None

    class DummyDeploymentService:
        def __init__(self, settings, repository, session):
            return None

        def submit(self, request, *, delete_eligible):
            attempted.append(request.unit_id)
            if request.unit_id == "bad-unit":
                return SimpleNamespace(
                    deployment_id="dep_bad",
                    status="failed",
                    failure_reason="synthetic failure",
                )
            return SimpleNamespace(deployment_id=f"dep_{request.unit_id}", status="healthy", failure_reason=None)

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("bad-unit", "later-unit"))
    monkeypatch.setattr(bootstrap_service, "ManagedGitRepository", lambda settings: SimpleNamespace(get_head_commit=lambda branch: "abc123"))
    monkeypatch.setattr(bootstrap_service, "get_session_factory", lambda: lambda: DummySession())
    monkeypatch.setattr(bootstrap_service, "DeploymentService", DummyDeploymentService)
    monkeypatch.setattr(bootstrap_service, "RegistryService", lambda session: object())
    bootstrapper = RuntimeBootstrapper(
        SimpleNamespace(main_worktree_dir=Path("/unused"), runtime_strategy="stub", runtime_bootstrap_max_parallel_deployments=2)
    )
    monkeypatch.setattr(
        bootstrapper,
        "_load_bootstrap_manifests",
        lambda: {"bad-unit": _manifest(), "later-unit": _manifest("bad-unit")},
    )
    monkeypatch.setattr(bootstrapper, "_current_deployment_id", lambda registry, service, unit_id, commit_sha: None)
    monkeypatch.setattr(bootstrapper, "_bootstrap_source_exists", lambda service, unit_id, commit_sha: True)

    result = bootstrapper.ensure_bootstrapped(fail_fast=False)

    assert attempted == ["bad-unit"]
    assert [(unit.unit_id, unit.status, unit.deployment_id) for unit in result.units] == [
        ("bad-unit", "failed", "dep_bad"),
        ("later-unit", "blocked", None),
    ]


def test_runtime_bootstrapper_failed_dependency_does_not_stop_unrelated_unit(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    attempted: list[str] = []

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            return None

    class DummyDeploymentService:
        def __init__(self, settings, repository, session):
            return None

        def submit(self, request, *, delete_eligible):
            attempted.append(request.unit_id)
            if request.unit_id == "bad-unit":
                return SimpleNamespace(
                    deployment_id="dep_bad",
                    status="failed",
                    failure_reason="synthetic failure",
                )
            return SimpleNamespace(deployment_id=f"dep_{request.unit_id}", status="healthy", failure_reason=None)

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("bad-unit", "dependent-unit", "independent-unit"))
    monkeypatch.setattr(bootstrap_service, "ManagedGitRepository", lambda settings: SimpleNamespace(get_head_commit=lambda branch: "abc123"))
    monkeypatch.setattr(bootstrap_service, "get_session_factory", lambda: lambda: DummySession())
    monkeypatch.setattr(bootstrap_service, "DeploymentService", DummyDeploymentService)
    monkeypatch.setattr(bootstrap_service, "RegistryService", lambda session: object())
    bootstrapper = RuntimeBootstrapper(
        SimpleNamespace(main_worktree_dir=Path("/unused"), runtime_strategy="stub", runtime_bootstrap_max_parallel_deployments=2)
    )
    monkeypatch.setattr(
        bootstrapper,
        "_load_bootstrap_manifests",
        lambda: {
            "bad-unit": _manifest(),
            "dependent-unit": _manifest("bad-unit"),
            "independent-unit": _manifest(),
        },
    )
    monkeypatch.setattr(bootstrapper, "_current_deployment_id", lambda registry, service, unit_id, commit_sha: None)
    monkeypatch.setattr(bootstrapper, "_bootstrap_source_exists", lambda service, unit_id, commit_sha: True)

    result = bootstrapper.ensure_bootstrapped(fail_fast=False)
    statuses = {unit.unit_id: unit.status for unit in result.units}

    assert statuses == {
        "bad-unit": "failed",
        "dependent-unit": "blocked",
        "independent-unit": "healthy",
    }
    assert set(attempted) == {"bad-unit", "independent-unit"}
    assert "dependent-unit" not in attempted


def test_runtime_bootstrapper_records_current_and_skipped_units(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    class DummySession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def commit(self):
            return None

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("current-unit", "missing-unit"))
    monkeypatch.setattr(bootstrap_service, "ManagedGitRepository", lambda settings: SimpleNamespace(get_head_commit=lambda branch: "abc123"))
    monkeypatch.setattr(bootstrap_service, "get_session_factory", lambda: lambda: DummySession())
    monkeypatch.setattr(bootstrap_service, "DeploymentService", lambda settings, repository, session: object())
    monkeypatch.setattr(bootstrap_service, "RegistryService", lambda session: object())
    bootstrapper = RuntimeBootstrapper(
        SimpleNamespace(main_worktree_dir=Path("/unused"), runtime_strategy="stub", runtime_bootstrap_max_parallel_deployments=2)
    )
    monkeypatch.setattr(
        bootstrapper,
        "_load_bootstrap_manifests",
        lambda: {"current-unit": _manifest(), "missing-unit": _manifest("current-unit")},
    )
    monkeypatch.setattr(
        bootstrapper,
        "_current_deployment_id",
        lambda registry, service, unit_id, commit_sha: "dep_current" if unit_id == "current-unit" else None,
    )
    monkeypatch.setattr(bootstrapper, "_bootstrap_source_exists", lambda service, unit_id, commit_sha: False)

    result = bootstrapper.ensure_bootstrapped(fail_fast=False)

    assert [(unit.unit_id, unit.status, unit.deployment_id, unit.failure_reason) for unit in result.units] == [
        ("current-unit", "current", "dep_current", None),
        ("missing-unit", "skipped", None, "bootstrap source missing"),
    ]


def test_dependency_plan_detects_cycle_and_blocks_downstream(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("a", "b", "c", "d", "e"))
    bootstrapper = RuntimeBootstrapper(SimpleNamespace())

    plan = bootstrapper._build_dependency_plan(
        {
            "a": _manifest("b"),
            "b": _manifest("c"),
            "c": _manifest("a"),
            "d": _manifest("a"),
            "e": _manifest(),
        }
    )

    assert plan.cycles[0].path == ("a", "b", "c", "a")
    assert {unit_id: blocked.reason for unit_id, blocked in plan.blocked.items()} == {
        "a": "dependency_cycle",
        "b": "dependency_cycle",
        "c": "dependency_cycle",
        "d": "dependency_blocked",
    }
    assert plan.dependency_issues()["cycles"][0]["path"] == ["a", "b", "c", "a"]


def test_ready_units_use_dependency_waves(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("a", "b", "c", "d"))
    bootstrapper = RuntimeBootstrapper(SimpleNamespace())
    plan = bootstrapper._build_dependency_plan(
        {
            "a": _manifest(),
            "b": _manifest(),
            "c": _manifest("a", "b"),
            "d": _manifest("c"),
        }
    )

    assert bootstrapper._ready_units(plan.dependencies, {}) == ["a", "b"]
    assert bootstrapper._ready_units(plan.dependencies, {"a": "healthy", "b": "current"}) == ["c"]
    assert bootstrapper._ready_units(plan.dependencies, {"a": "healthy", "b": "current", "c": "healthy"}) == ["d"]


def test_unknown_dependency_blocks_affected_unit_only(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("a", "b"))
    bootstrapper = RuntimeBootstrapper(SimpleNamespace())
    plan = bootstrapper._build_dependency_plan({"a": _manifest("missing-service"), "b": _manifest()})

    assert plan.blocked["a"].reason == "unknown_dependency"
    assert "b" not in plan.blocked
    assert plan.dependency_issues()["invalid_dependencies"] == [{"unit_id": "a", "dependency": "missing-service"}]


def test_status_tracker_records_blocked_units(monkeypatch) -> None:
    from app.services import bootstrap_service
    from app.services.bootstrap_service import RuntimeBootstrapper

    calls: list[tuple[str, str]] = []

    class Tracker:
        def mark_unit_deploying(self, run_id, unit_id):
            calls.append(("deploying", unit_id))

        def mark_unit_current(self, run_id, unit_id, deployment_id=None):
            calls.append(("current", unit_id))

        def mark_unit_skipped(self, run_id, unit_id, reason=None):
            calls.append(("skipped", unit_id))

        def mark_unit_healthy(self, run_id, unit_id, deployment_id=None):
            calls.append(("healthy", unit_id))

        def mark_unit_failed(self, run_id, unit_id, reason, deployment_id=None):
            calls.append(("failed", unit_id))

        def mark_unit_blocked(self, run_id, unit_id, reason, deployment_id=None):
            calls.append(("blocked", f"{unit_id}:{reason}"))

        def set_dependency_issues(self, run_id, dependency_issues):
            calls.append(("issues", str(len(dependency_issues["blocked_units"]))))

    monkeypatch.setattr(bootstrap_service, "BOOTSTRAP_UNITS", ("a", "b"))
    monkeypatch.setattr(bootstrap_service, "ManagedGitRepository", lambda settings: SimpleNamespace(get_head_commit=lambda branch: "abc123"))
    monkeypatch.setattr(bootstrap_service, "get_session_factory", lambda: None)
    bootstrapper = RuntimeBootstrapper(SimpleNamespace(runtime_bootstrap_max_parallel_deployments=1))
    monkeypatch.setattr(bootstrapper, "_load_bootstrap_manifests", lambda: {"a": _manifest("b"), "b": _manifest("a")})

    result = bootstrapper.ensure_bootstrapped(status_tracker=Tracker(), run_id=1)

    assert {unit.status for unit in result.units} == {"blocked"}
    assert any(call[0] == "blocked" and "Blocked by dependency cycle" in call[1] for call in calls)
