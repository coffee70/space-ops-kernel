"""Health API."""

from __future__ import annotations

from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "runtime_root": str(settings.resolved_runtime_root),
        "managed_fork_ready": settings.bare_repo_dir.exists() and settings.main_worktree_dir.exists(),
        "workspace_root": str(settings.main_worktree_dir / "project"),
    }

