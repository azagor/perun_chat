"""
PerunChat social graph: friends, blocked lists, and friend requests.

All agent IDs are normalised to lower case on storage and lookup so display
names stay case-sensitive while comparison is not.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import db as db_module


class SocialGraphError(ValueError):
    """Domain error raised by the social graph (invalid state, duplicate, etc)."""


class SocialGraph:
    """SQLite-backed store for friendships, blocks, and friend requests."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        if db_path == ":memory:":
            self._db_token = uuid.uuid4().hex
            self._shared_uri = f"file:mem_social_{self._db_token}?mode=memory&cache=shared"
            self._keepalive = db_module._connect(self._shared_uri)
            self._keepalive.row_factory = sqlite3.Row
        else:
            self._shared_uri = db_path
            self._keepalive = None
        db_module.init_db(self._shared_uri)

    def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    def _db_uri(self) -> str:
        return self._shared_uri

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _norm(agent_id: str) -> str:
        return agent_id.lower()

    @staticmethod
    def _ordered_pair(a: str, b: str) -> Tuple[str, str]:
        return tuple(sorted((a.lower(), b.lower())))  # type: ignore[return-type]

    def request_friend(self, from_agent_id: str, to_agent_id: str) -> Dict[str, Any]:
        """Create or re-activate a pending friend request from one agent to another."""
        from_id = self._norm(from_agent_id)
        to_id = self._norm(to_agent_id)
        if from_id == to_id:
            raise SocialGraphError("cannot_friend_request_self")
        if self.are_friends(from_id, to_id):
            raise SocialGraphError("already_friends")
        if self.is_blocked(from_id, to_id):
            raise SocialGraphError("blocked_relationship")

        now = self._now()
        request_id = f"fr_{uuid.uuid4().hex[:12]}"

        with db_module.db(self._db_uri()) as conn:
            # Check for an existing row between the same ordered pair. If there is
            # already a pending request, fail. Otherwise update any prior rejected /
            # cancelled row back to pending with a fresh request id.
            existing = conn.execute(
                """
                SELECT * FROM friend_requests
                WHERE from_agent_id = ? AND to_agent_id = ?
                ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, updated_at DESC
                """,
                (from_id, to_id),
            ).fetchone()
            if existing:
                if existing["status"] == "pending":
                    raise SocialGraphError("friend_request_already_exists")
                conn.execute(
                    """
                    UPDATE friend_requests
                    SET request_id = ?, status = 'pending', created_at = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (request_id, now, now, existing["request_id"]),
                )
                conn.commit()
                return {
                    "request_id": request_id,
                    "from_agent_id": from_id,
                    "to_agent_id": to_id,
                    "status": "pending",
                    "created_at": now,
                    "updated_at": now,
                }

            try:
                conn.execute(
                    """
                    INSERT INTO friend_requests (request_id, from_agent_id, to_agent_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (request_id, from_id, to_id, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise SocialGraphError("friend_request_already_exists") from exc
        return {
            "request_id": request_id,
            "from_agent_id": from_id,
            "to_agent_id": to_id,
            "status": "pending",
            "created_at": now,
            "updated_at": now,
        }

    def accept_friend(self, request_id: str, by_agent_id: str) -> Dict[str, Any]:
        """Accept a pending friend request. Only the target may accept."""
        by = self._norm(by_agent_id)
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                "SELECT * FROM friend_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise SocialGraphError("friend_request_not_found")
            if row["status"] != "pending":
                raise SocialGraphError(f"friend_request_already_{row['status']}")
            if self._norm(row["to_agent_id"]) != by:
                raise SocialGraphError("not_request_recipient")

            now = self._now()
            a, b = self._ordered_pair(row["from_agent_id"], row["to_agent_id"])
            conn.execute(
                "UPDATE friend_requests SET status = 'accepted', updated_at = ? WHERE request_id = ?",
                (now, request_id),
            )
            try:
                conn.execute(
                    "INSERT INTO friendships (agent_a, agent_b, created_at) VALUES (?, ?, ?)",
                    (a, b, now),
                )
            except sqlite3.IntegrityError:
                # Friendship already existed; keep idempotent.
                pass
            conn.commit()
            return {
                "request_id": request_id,
                "from_agent_id": row["from_agent_id"],
                "to_agent_id": row["to_agent_id"],
                "status": "accepted",
                "updated_at": now,
            }

    def reject_friend(self, request_id: str, by_agent_id: str) -> Dict[str, Any]:
        """Reject or cancel a friend request."""
        by = self._norm(by_agent_id)
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                "SELECT * FROM friend_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise SocialGraphError("friend_request_not_found")
            if row["status"] != "pending":
                raise SocialGraphError(f"friend_request_already_{row['status']}")
            if by not in (self._norm(row["from_agent_id"]), self._norm(row["to_agent_id"])):
                raise SocialGraphError("not_request_participant")
            new_status = "cancelled" if by == self._norm(row["from_agent_id"]) else "rejected"
            now = self._now()
            conn.execute(
                "UPDATE friend_requests SET status = ?, updated_at = ? WHERE request_id = ?",
                (new_status, now, request_id),
            )
            conn.commit()
            return {
                "request_id": request_id,
                "from_agent_id": row["from_agent_id"],
                "to_agent_id": row["to_agent_id"],
                "status": new_status,
                "updated_at": now,
            }

    def remove_friend(self, agent_id: str, friend_agent_id: str) -> Dict[str, Any]:
        """Remove an established friendship."""
        a, b = self._ordered_pair(agent_id, friend_agent_id)
        with db_module.db(self._db_uri()) as conn:
            cur = conn.execute(
                "DELETE FROM friendships WHERE agent_a = ? AND agent_b = ?",
                (a, b),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise SocialGraphError("not_friends")
            return {
                "agent_id": agent_id,
                "friend_agent_id": friend_agent_id,
                "status": "removed",
            }

    def list_friends(self, agent_id: str) -> List[str]:
        """Return the agent IDs of all confirmed friends."""
        me = self._norm(agent_id)
        with db_module.db(self._db_uri()) as conn:
            rows = conn.execute(
                """
                SELECT agent_a, agent_b FROM friendships
                WHERE agent_a = ? OR agent_b = ?
                ORDER BY created_at DESC
                """,
                (me, me),
            ).fetchall()
            friends: List[str] = []
            for row in rows:
                other = row["agent_a"] if row["agent_b"] == me else row["agent_b"]
                friends.append(other)
            return friends

    def list_requests(
        self,
        agent_id: str,
        direction: str = "all",
    ) -> List[Dict[str, Any]]:
        """Return pending friend requests for an agent."""
        me = self._norm(agent_id)
        if direction == "incoming":
            where = "to_agent_id = ? AND status = 'pending'"
            params: Tuple[Any, ...] = (me,)
        elif direction == "outgoing":
            where = "from_agent_id = ? AND status = 'pending'"
            params = (me,)
        elif direction == "all":
            where = "(from_agent_id = ? OR to_agent_id = ?) AND status = 'pending'"
            params = (me, me)
        else:
            raise SocialGraphError(f"invalid_direction:{direction}")

        with db_module.db(self._db_uri()) as conn:
            rows = conn.execute(
                f"SELECT * FROM friend_requests WHERE {where} ORDER BY created_at DESC",  # noqa: S608
                params,
            ).fetchall()
            return [db_module.row_to_dict(row) for row in rows]

    def are_friends(self, agent_a: str, agent_b: str) -> bool:
        """Return True iff the two agents have an accepted friendship."""
        a, b = self._ordered_pair(agent_a, agent_b)
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                "SELECT 1 FROM friendships WHERE agent_a = ? AND agent_b = ?",
                (a, b),
            ).fetchone()
            return row is not None

    def block(self, blocker_agent_id: str, blocked_agent_id: str) -> Dict[str, Any]:
        """Block an agent. Existing friendship is dissolved."""
        if self._norm(blocker_agent_id) == self._norm(blocked_agent_id):
            raise SocialGraphError("cannot_block_self")
        blocker = self._norm(blocker_agent_id)
        blocked = self._norm(blocked_agent_id)
        now = self._now()
        with db_module.db(self._db_uri()) as conn:
            # Dissolve any friendship in either direction.
            a, b = self._ordered_pair(blocker, blocked)
            conn.execute(
                "DELETE FROM friendships WHERE agent_a = ? AND agent_b = ?",
                (a, b),
            )
            # Mark any pending request between the pair as cancelled/rejected.
            conn.execute(
                """
                UPDATE friend_requests
                SET status = CASE
                    WHEN from_agent_id = ? THEN 'cancelled'
                    ELSE 'rejected'
                END,
                updated_at = ?
                WHERE status = 'pending'
                AND ((from_agent_id = ? AND to_agent_id = ?)
                     OR (from_agent_id = ? AND to_agent_id = ?))
                """,
                (blocker, now, blocker, blocked, blocked, blocker),
            )
            try:
                conn.execute(
                    "INSERT INTO blocked_agents (blocker_agent_id, blocked_agent_id, created_at) VALUES (?, ?, ?)",
                    (blocker, blocked, now),
                )
            except sqlite3.IntegrityError as exc:
                raise SocialGraphError("already_blocked") from exc
            conn.commit()
            return {
                "blocker_agent_id": blocker,
                "blocked_agent_id": blocked,
                "status": "blocked",
                "created_at": now,
            }

    def unblock(self, blocker_agent_id: str, blocked_agent_id: str) -> Dict[str, Any]:
        """Remove a block."""
        blocker = self._norm(blocker_agent_id)
        blocked = self._norm(blocked_agent_id)
        with db_module.db(self._db_uri()) as conn:
            cur = conn.execute(
                "DELETE FROM blocked_agents WHERE blocker_agent_id = ? AND blocked_agent_id = ?",
                (blocker, blocked),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise SocialGraphError("not_blocked")
            return {
                "blocker_agent_id": blocker,
                "blocked_agent_id": blocked,
                "status": "unblocked",
            }

    def list_blocked(self, blocker_agent_id: str) -> List[str]:
        """Return the list of agents blocked by the given agent."""
        with db_module.db(self._db_uri()) as conn:
            rows = conn.execute(
                "SELECT blocked_agent_id FROM blocked_agents WHERE blocker_agent_id = ? ORDER BY created_at DESC",
                (self._norm(blocker_agent_id),),
            ).fetchall()
            return [row["blocked_agent_id"] for row in rows]

    def is_blocked(self, viewer_agent_id: str, target_agent_id: str) -> bool:
        """Return True iff either agent has blocked the other."""
        a = self._norm(viewer_agent_id)
        b = self._norm(target_agent_id)
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM blocked_agents
                WHERE (blocker_agent_id = ? AND blocked_agent_id = ?)
                   OR (blocker_agent_id = ? AND blocked_agent_id = ?)
                """,
                (a, b, b, a),
            ).fetchone()
            return row is not None


__all__ = ["SocialGraph", "SocialGraphError"]
