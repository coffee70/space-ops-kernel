"""Postgres-backed runtime bootstrap status tracking."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import sessionmaker

from app.db import get_session_factory
from app.models.runtime import RuntimeBootstrapRun, RuntimeBootstrapUnit, utcnow

RUN_NOT_STARTED = "not_started"
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_COMPLETED_WITH_FAILURES = "completed_with_failures"
RUN_FAILED = "failed"

UNIT_PENDING = "pending"
UNIT_SKIPPED = "skipped"
UNIT_CURRENT = "current"
UNIT_DEPLOYING = "deploying"
UNIT_HEALTHY = "healthy"
UNIT_FAILED = "failed"

UNIT_STATUSES = (UNIT_PENDING, UNIT_SKIPPED, UNIT_CURRENT, UNIT_DEPLOYING, UNIT_HEALTHY, UNIT_FAILED)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


class RuntimeBootstrapStatusService:
    """Small transactional API for runtime bootstrap state."""

    def __init__(self, session_factory: sessionmaker | None = None):
        self.session_factory = session_factory or get_session_factory()

    def start_run(self, units: Sequence[str]) -> int:
        now = utcnow()
        with self.session_factory() as session:
            for previous in session.query(RuntimeBootstrapRun).filter(RuntimeBootstrapRun.status == RUN_RUNNING).all():
                previous.status = RUN_FAILED
                previous.completed_at = now
                previous.updated_at = now
                previous.failure_reason = previous.failure_reason or "interrupted by control-plane restart"

            run = RuntimeBootstrapRun(status=RUN_RUNNING, started_at=now, created_at=now, updated_at=now)
            session.add(run)
            session.flush()
            run_id = run.id
            for unit_id in units:
                session.add(
                    RuntimeBootstrapUnit(
                        run_id=run_id,
                        unit_id=unit_id,
                        status=UNIT_PENDING,
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.commit()
            return run_id

    def mark_unit_deploying(self, run_id: int, unit_id: str) -> None:
        self._mark_unit(run_id, unit_id, status=UNIT_DEPLOYING, started=True, completed=False)

    def mark_unit_current(self, run_id: int, unit_id: str, deployment_id: str | None = None) -> None:
        self._mark_unit(run_id, unit_id, status=UNIT_CURRENT, deployment_id=deployment_id, started=True, completed=True)

    def mark_unit_skipped(self, run_id: int, unit_id: str, reason: str | None = None) -> None:
        self._mark_unit(run_id, unit_id, status=UNIT_SKIPPED, failure_reason=reason, started=True, completed=True)

    def mark_unit_healthy(self, run_id: int, unit_id: str, deployment_id: str | None = None) -> None:
        self._mark_unit(run_id, unit_id, status=UNIT_HEALTHY, deployment_id=deployment_id, started=True, completed=True)

    def mark_unit_failed(
        self,
        run_id: int,
        unit_id: str,
        reason: str,
        deployment_id: str | None = None,
    ) -> None:
        self._mark_unit(
            run_id,
            unit_id,
            status=UNIT_FAILED,
            deployment_id=deployment_id,
            failure_reason=reason,
            started=True,
            completed=True,
        )

    def finish_run(self, run_id: int, failure_reason: str | None = None) -> None:
        now = utcnow()
        with self.session_factory() as session:
            run = session.get(RuntimeBootstrapRun, run_id)
            if run is None:
                return
            failed_count = (
                session.query(RuntimeBootstrapUnit)
                .filter(RuntimeBootstrapUnit.run_id == run_id, RuntimeBootstrapUnit.status == UNIT_FAILED)
                .count()
            )
            run.status = RUN_FAILED if failure_reason else RUN_COMPLETED_WITH_FAILURES if failed_count else RUN_COMPLETED
            run.completed_at = now
            run.updated_at = now
            run.failure_reason = failure_reason
            session.commit()

    def get_latest_run_snapshot(self) -> dict:
        with self.session_factory() as session:
            run = session.query(RuntimeBootstrapRun).order_by(RuntimeBootstrapRun.started_at.desc(), RuntimeBootstrapRun.id.desc()).first()
            if run is None:
                return {
                    "status": RUN_NOT_STARTED,
                    "started_at": None,
                    "completed_at": None,
                    "summary": {status: 0 for status in UNIT_STATUSES},
                    "units": [],
                }
            units = (
                session.query(RuntimeBootstrapUnit)
                .filter(RuntimeBootstrapUnit.run_id == run.id)
                .order_by(RuntimeBootstrapUnit.id.asc())
                .all()
            )
            summary = {status: 0 for status in UNIT_STATUSES}
            summary.update(Counter(unit.status for unit in units))
            return {
                "run_id": run.id,
                "status": run.status,
                "started_at": _iso(run.started_at),
                "completed_at": _iso(run.completed_at),
                "failure_reason": run.failure_reason,
                "summary": summary,
                "units": [
                    {
                        "unit_id": unit.unit_id,
                        "status": unit.status,
                        "deployment_id": unit.deployment_id,
                        "failure_reason": unit.failure_reason,
                        "started_at": _iso(unit.started_at),
                        "completed_at": _iso(unit.completed_at),
                    }
                    for unit in units
                ],
            }

    def _mark_unit(
        self,
        run_id: int,
        unit_id: str,
        *,
        status: str,
        deployment_id: str | None = None,
        failure_reason: str | None = None,
        started: bool,
        completed: bool,
    ) -> None:
        now = utcnow()
        with self.session_factory() as session:
            unit = (
                session.query(RuntimeBootstrapUnit)
                .filter(RuntimeBootstrapUnit.run_id == run_id, RuntimeBootstrapUnit.unit_id == unit_id)
                .one_or_none()
            )
            if unit is None:
                unit = RuntimeBootstrapUnit(run_id=run_id, unit_id=unit_id, status=UNIT_PENDING, created_at=now)
                session.add(unit)
            unit.status = status
            unit.updated_at = now
            if deployment_id is not None:
                unit.deployment_id = deployment_id
            unit.failure_reason = failure_reason
            if started and unit.started_at is None:
                unit.started_at = now
            if completed:
                unit.completed_at = now
            session.commit()
