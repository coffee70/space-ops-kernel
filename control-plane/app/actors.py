"""Actor context seams for future auth/policy insertion."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(slots=True)
class ActorContext:
    """Request actor metadata."""

    actor_id: str
    display_name: str
    anonymous: bool = True


def get_actor_context(request: Request) -> ActorContext:
    """Resolve the current request actor."""

    actor_id = request.headers.get("X-Actor-Id") or "anonymous"
    display_name = request.headers.get("X-Actor-Name") or "Anonymous"
    return ActorContext(actor_id=actor_id, display_name=display_name, anonymous=actor_id == "anonymous")

