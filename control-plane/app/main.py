"""FastAPI entry point for the Space Ops control plane."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import code, delete, deployments, health, registry, templates
from app.config import get_settings
from app.db import ensure_database_exists
from app.migrations import run_migrations
from app.services.bootstrap_service import (
    ApplicationRegistryBootstrapper,
    ManagedForkBootstrapper,
    RuntimeBootstrapper,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_runtime_dirs()
    ensure_database_exists()
    run_migrations()
    ManagedForkBootstrapper(settings).ensure_bootstrapped()
    ApplicationRegistryBootstrapper(settings).ensure_seeded()
    RuntimeBootstrapper(settings).ensure_bootstrapped()
    yield


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
app.include_router(code.router)
app.include_router(delete.router)
app.include_router(templates.router)
app.include_router(deployments.router)
app.include_router(registry.router)
app.include_router(registry.proxy_router)
app.include_router(registry.internal_proxy_router)
