"""Tests for Compose fragment command emission."""

from __future__ import annotations

from app.deployments.service import _compose_safe_run_command


def test_compose_safe_run_command_unwraps_sh_c_double_quoted() -> None:
    raw = 'sh -c "cd /app/platform/backend && uvicorn main:app --app-dir services/foo --host 0.0.0.0 --port 8080"'
    assert _compose_safe_run_command(raw) == [
        "sh",
        "-c",
        "cd /app/platform/backend && uvicorn main:app --app-dir services/foo --host 0.0.0.0 --port 8080",
    ]


def test_compose_safe_run_command_passes_through_short_commands() -> None:
    assert _compose_safe_run_command("node dist/server.js") == "node dist/server.js"


def test_compose_safe_run_command_strips_whitespace() -> None:
    raw = '  sh -c "echo ok"  '
    assert _compose_safe_run_command(raw) == ["sh", "-c", "echo ok"]
