"""FastAPI entry point for the Space Ops control plane."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import bootstrap, code, delete, deployments, health, registry, templates
from app.config import get_settings
from app.db import ensure_database_exists
from app.migrations import run_migrations
from app.services.bootstrap_service import (
    ApplicationRegistryBootstrapper,
    BOOTSTRAP_UNITS,
    ManagedForkBootstrapper,
    RuntimeBootstrapper,
)
from app.services.bootstrap_status import RuntimeBootstrapStatusService

logger = logging.getLogger(__name__)


def run_runtime_bootstrap_background(settings, stop_event: threading.Event) -> None:
    """Run runtime bootstrap without making FastAPI startup depend on managed units."""

    status_service = RuntimeBootstrapStatusService()
    run_id = status_service.start_run(BOOTSTRAP_UNITS)
    if stop_event.is_set():
        status_service.finish_run(run_id, failure_reason="control-plane shutdown requested before bootstrap started")
        return
    try:
        RuntimeBootstrapper(settings).ensure_bootstrapped(
            fail_fast=False,
            status_tracker=status_service,
            run_id=run_id,
        )
        status_service.finish_run(run_id)
    except Exception as exc:
        logger.exception("runtime bootstrap failed")
        status_service.finish_run(run_id, failure_reason=str(exc) or type(exc).__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_runtime_dirs()
    ensure_database_exists()
    run_migrations()
    ManagedForkBootstrapper(settings).ensure_bootstrapped()
    ApplicationRegistryBootstrapper(settings).ensure_seeded()
    RuntimeBootstrapper(settings).seed_bootstrap_unit_registry()
    if os.environ.get("CONTROL_PLANE_SKIP_BACKGROUND_RUNTIME_BOOTSTRAP") == "1":
        yield
        return
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_runtime_bootstrap_background,
        args=(settings, stop_event),
        name="runtime-bootstrap",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=10.0)


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(bootstrap.router)
app.include_router(code.router)
app.include_router(delete.router)
app.include_router(templates.router)
app.include_router(deployments.router)
app.include_router(registry.router)
app.include_router(registry.proxy_router)
app.include_router(registry.internal_proxy_router)
