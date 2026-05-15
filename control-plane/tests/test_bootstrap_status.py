from __future__ import annotations

import importlib
from pathlib import Path


def _prepare_database() -> None:
    import app.config
    import app.db
    import app.migrations

    app.config.get_settings.cache_clear()
    app.db._engine = None
    app.db._SessionLocal = None
    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.migrations)
    app.migrations.run_migrations(app.db.get_engine())


def test_bootstrap_status_initial_snapshot(control_plane_env: Path) -> None:
    _prepare_database()
    from app.services.bootstrap_status import RuntimeBootstrapStatusService

    snapshot = RuntimeBootstrapStatusService().get_latest_run_snapshot()

    assert snapshot["status"] == "not_started"
    assert snapshot["summary"]["pending"] == 0
    assert snapshot["summary"]["blocked"] == 0
    assert snapshot["dependency_issues"] == {"cycles": [], "blocked_units": [], "invalid_dependencies": []}
    assert snapshot["units"] == []


def test_bootstrap_status_tracks_unit_transitions(control_plane_env: Path) -> None:
    _prepare_database()
    from app.services.bootstrap_status import RuntimeBootstrapStatusService

    service = RuntimeBootstrapStatusService()
    run_id = service.start_run(["alpha-service", "beta-service", "gamma-service", "delta-service"])
    assert isinstance(run_id, int)
    service.mark_unit_current(run_id, "alpha-service", "dep_alpha")
    service.mark_unit_deploying(run_id, "beta-service")
    service.mark_unit_failed(run_id, "beta-service", "failed health check", "dep_beta")
    service.mark_unit_skipped(run_id, "gamma-service", "missing source")
    service.mark_unit_blocked(run_id, "delta-service", "dependency failed")
    service.set_dependency_issues(
        run_id,
        {
            "cycles": [{"units": ["a", "b"], "path": ["a", "b", "a"]}],
            "blocked_units": [{"unit_id": "delta-service", "reason": "dependency_failed", "blocking_units": ["beta-service"]}],
            "invalid_dependencies": [],
        },
    )
    service.finish_run(run_id)

    snapshot = service.get_latest_run_snapshot()

    assert snapshot["status"] == "completed_with_failures"
    assert snapshot["summary"] == {
        "pending": 0,
        "skipped": 1,
        "current": 1,
        "deploying": 0,
        "healthy": 0,
        "failed": 1,
        "blocked": 1,
    }
    assert snapshot["units"][0]["deployment_id"] == "dep_alpha"
    assert snapshot["units"][1]["failure_reason"] == "failed health check"
    assert snapshot["units"][2]["status"] == "skipped"
    assert snapshot["units"][3]["status"] == "blocked"
    assert snapshot["dependency_issues"]["cycles"][0]["path"] == ["a", "b", "a"]
