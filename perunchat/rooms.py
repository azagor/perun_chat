"""
Room taxonomy helpers and ACL validation.

PerunChat room IDs:
  priv:<sha256(min(A, B) + ":" + max(A, B))>
  grp:<tenant_id>:<project_id>:<slug>
  pub:<tenant_id>:<channel_slug>
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from typing import Optional, Tuple


ROOM_RE = re.compile(r"^(priv|private|grp|pub):[a-zA-Z0-9._:-]+$")


def parse_room_id(room_id: str) -> Tuple[str, str]:
    """Return (scope, body) or raise ValueError.

    ``private:`` is accepted as an alias for ``priv:`` so older clients that
    used the longer prefix continue to work without crashing the gateway.
    """
    if not ROOM_RE.match(room_id):
        raise ValueError(f"invalid room_id: {room_id}")
    scope, body = room_id.split(":", 1)
    if scope == "private":
        scope = "priv"
    return scope, body


def private_room_id(agent_a: str, agent_b: str) -> str:
    """Deterministic private room ID for two agent IDs (case-insensitive)."""
    a, b = sorted((agent_a.lower(), agent_b.lower()))
    digest = hashlib.sha256(f"{a}:{b}".encode()).hexdigest()
    return f"priv:{digest}"


def group_room_id(tenant_id: str, project_id: str, slug: str) -> str:
    return f"grp:{tenant_id}:{project_id}:{slug}"


def public_room_id(tenant_id: str, channel_slug: str) -> str:
    return f"pub:{tenant_id}:{channel_slug}"


import fnmatch


class RoomResolver:
    """Resolve room membership from ACL records and key scopes."""

    def __init__(self) -> None:
        self._acls: dict[str, set[str]] = {}

    def set_members(self, room_id: str, agent_ids: list[str]) -> None:
        self._acls[room_id] = set(agent_ids)

    def add_member(self, room_id: str, agent_id: str) -> None:
        self._acls.setdefault(room_id, set()).add(agent_id)

    def _is_allowed_by_scope(self, room_id: str, scopes: list[str]) -> bool:
        """Return True if any of the agent's scopes matches the room_id."""
        for scope in scopes:
            if fnmatch.fnmatch(room_id, scope):
                return True
        return False

    def can_join(self, room_id: str, agent_id: str, scopes: list[str] | None = None) -> bool:
        scope, _ = parse_room_id(room_id)
        if scope == "pub":
            # Public rooms require a matching scope (e.g. pub:global:*).
            if scopes is not None and not self._is_allowed_by_scope(room_id, scopes):
                return False
            return True
        if scope == "priv":
            return agent_id in self._acls.get(room_id, set())
        if scope == "grp":
            # Explicit ACL takes precedence, otherwise fall back to scopes.
            members = self._acls.get(room_id, set())
            if members:
                return agent_id in members
            if scopes is not None:
                return self._is_allowed_by_scope(room_id, scopes)
            return False
        return False


__all__ = [
    "parse_room_id",
    "private_room_id",
    "group_room_id",
    "public_room_id",
    "RoomResolver",
]
