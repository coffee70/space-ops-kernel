from __future__ import annotations

import re
from pathlib import Path


def test_edge_proxy_routes_system_requests_to_control_plane() -> None:
    caddyfile = Path(__file__).resolve().parents[2] / "platform-edge-proxy" / "Caddyfile"
    content = caddyfile.read_text(encoding="utf-8")

    assert re.search(r"handle /system/\* \{\s+reverse_proxy control-plane:8100\s+\}", content)
