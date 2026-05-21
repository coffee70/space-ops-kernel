from __future__ import annotations

import subprocess
from pathlib import Path


def test_preview_banner_demo_script_is_valid_bash() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "preview-banner-demo.sh"

    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
