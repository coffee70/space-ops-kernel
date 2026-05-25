"""Asynchronous deployment worker."""

from __future__ import annotations

import logging
import time

from app.config import Settings, get_settings
from app.db import ensure_database_exists, get_engine, get_session_factory
from app.deployments.service import DeploymentService
from app.git.repository import ManagedGitRepository
from app.migrations import run_migrations
from app.registry.service import RegistryService

logger = logging.getLogger(__name__)


class DeploymentWorker:
    """Claim queued deployment records and execute them one at a time."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.repository = ManagedGitRepository(settings)

    def cleanup_stale_jobs(self) -> int:
        session_factory = get_session_factory()
        with session_factory() as session:
            registry = RegistryService(session)
            count = registry.fail_stale_deployments(
                older_than_minutes=self.settings.deployment_worker_stale_after_minutes,
                logs_root=self.settings.deployment_logs_root,
            )
            session.commit()
            if count:
                logger.warning("marked stale deployments failed: count=%s", count)
            return count

    def run_once(self) -> str | None:
        session_factory = get_session_factory()
        with session_factory() as session:
            registry = RegistryService(session)
            deployment = registry.claim_next_queued_deployment()
            if deployment is None:
                session.commit()
                return None
            deployment_id = deployment.deployment_id
            session.commit()

        with session_factory() as session:
            service = DeploymentService(self.settings, self.repository, session)
            service.execute_deployment(deployment_id)
            session.commit()
        return deployment_id

    def run_forever(self) -> None:
        self.cleanup_stale_jobs()
        while True:
            deployment_id = self.run_once()
            if deployment_id is None:
                time.sleep(self.settings.deployment_worker_poll_interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    settings.ensure_runtime_dirs()
    ensure_database_exists(settings.resolved_database_url)
    run_migrations(get_engine())
    DeploymentWorker(settings).run_forever()


if __name__ == "__main__":
    main()
