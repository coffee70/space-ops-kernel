"""FastAPI entry point for the Space Ops control plane."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import code, deployments, health, registry, templates
from app.config import get_settings
from app.migrations import run_migrations
from app.services.bootstrap_service import ManagedForkBootstrapper


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.ensure_runtime_dirs()
    run_migrations()
    ManagedForkBootstrapper(settings).ensure_bootstrapped()
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
app.include_router(templates.router)
app.include_router(deployments.router)
app.include_router(registry.router)
app.include_router(registry.proxy_router)
