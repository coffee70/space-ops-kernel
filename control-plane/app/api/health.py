"""Health API."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings
from app.services.bootstrap_status import RuntimeBootstrapStatusService

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    bootstrap = RuntimeBootstrapStatusService().get_latest_run_snapshot()
    summary = bootstrap.get("summary", {})
    return {
        "status": "ok",
        "runtime_root": str(settings.resolved_runtime_root),
        "managed_fork_ready": settings.bare_repo_dir.exists() and settings.main_worktree_dir.exists(),
        "workspace_root": str(settings.main_worktree_dir / "project"),
        "runtime_bootstrap": {
            "status": bootstrap["status"],
            "healthy": summary.get("healthy", 0),
            "current": summary.get("current", 0),
            "failed": summary.get("failed", 0),
            "pending": summary.get("pending", 0),
            "deploying": summary.get("deploying", 0),
            "skipped": summary.get("skipped", 0),
        },
    }
