"""Subprocess helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess and capture text output."""

    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=text,
        capture_output=True,
        check=True,
        timeout=timeout,
    )
