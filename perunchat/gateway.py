"""
PerunChat WebSocket Ingress Gateway

FastAPI/WebSocket entry point for agent chat frames. Integrates the security
pipeline (rate limiting, sequence validation, turn counters, DLP), key
authentication, and room broadcasting. Runtime-agnostic: any agent runtime that
sends the correct headers and frame schema can connect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import Depends, FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("perunchat.gateway")

from . import config, db as db_module
from .api import (
    _persist_room_member,
    hitl_engine,
    keystore,
    pending_proposals,
    router,
    social_graph,
)
from .keys import KeyStore
from .models import ChatFrame
from .rooms import RoomResolver, parse_room_id, private_room_id
from .security import SecurityPipeline


# Per-room message history cap and max payload size for backpressure.
_MAX_ROOM_HISTORY = 200
_MAX_PAYLOAD_BYTES = 8192


class Room:
    """In-memory room for development; replaces with Redis/NATS in Phase 2.

    Keeps a bounded ring-buffer of recent messages so late-joining clients
    (e.g. the public live preview) can catch up without an unbounded memory
    footprint.
    """

    def __init__(self, room_id: str, room_type: str) -> None:
        self.room_id = room_id
        self.room_type = room_type
        self.connections: Dict[WebSocket, str] = {}
        self._history: deque[Dict[str, Any]] = deque(maxlen=_MAX_ROOM_HISTORY)

    async def join(self, ws: WebSocket, agent_id: str) -> None:
        self.connections[ws] = agent_id

    async def leave(self, ws: WebSocket) -> None:
        self.connections.pop(ws, None)

    async def broadcast(self, sender: WebSocket, frame: Dict[str, Any]) -> None:
        sender_agent_id = self.connections.get(sender)
        message = json.dumps(frame, ensure_ascii=False)
        # Only store real chat frames for late-joiners; ephemeral control frames
        # such as PRESENCE_UPDATE should not be replayed.
        if (frame.get("sender") or {}).get("agent_id"):
            self._history.append(frame)
        dead: Set[WebSocket] = set()
        for conn, recipient_agent_id in list(self.connections.items()):
            # Respect bilateral blocks: don't deliver to a recipient who has
            # blocked the sender, and don't deliver from a sender who has
            # blocked the recipient.
            graph = gateway.social_graph
            if sender_agent_id and recipient_agent_id and graph is not None:
                if graph.is_blocked(recipient_agent_id, sender_agent_id):
                    continue
            try:
                await conn.send_text(message)
            except Exception:
                dead.add(conn)
        for conn in dead:
            self.connections.pop(conn, None)

    async def replay_history(self, ws: WebSocket, agent_id: str) -> None:
        """Send the buffered recent history to a newly joined connection."""
        if not self._history:
            return
        graph = gateway.social_graph
        for frame in self._history:
            sender_id = (frame.get("sender") or {}).get("agent_id")
            # Skip messages from/blocked for this recipient.
            if sender_id and graph is not None:
                if graph.is_blocked(agent_id, sender_id):
                    continue
            try:
                await ws.send_text(json.dumps(frame, ensure_ascii=False))
            except Exception:
                break


class ChatGateway:
    """State container attached to the FastAPI app."""

    def __init__(
        self,
        keystore: KeyStore,
        security: SecurityPipeline,
        rooms: RoomResolver,
        social_graph: Optional[Any] = None,
    ) -> None:
        self.keystore = keystore
        self.security = security
        self.rooms = rooms
        self.social_graph = social_graph
        self._rooms: Dict[str, Room] = {}
        self.lock = asyncio.Lock()
        # agent_id -> set of active WebSocket connections
        self._presence: Dict[str, Set[WebSocket]] = {}

    async def ensure_room(self, room_id: str, room_type: str) -> Room:
        async with self.lock:
            if room_id not in self._rooms:
                self._rooms[room_id] = Room(room_id, room_type)
            return self._rooms[room_id]

    async def drop_room(self, room_id: str) -> None:
        async with self.lock:
            self._rooms.pop(room_id, None)

    def mark_online(self, agent_id: str, ws: WebSocket) -> bool:
        """Register a connection as online. Returns True if this is the first
        connection for the agent (status changed from offline to online)."""
        conns = self._presence.setdefault(agent_id, set())
        changed = len(conns) == 0
        conns.add(ws)
        return changed

    def mark_offline(self, agent_id: str, ws: WebSocket) -> bool:
        """Unregister a connection. Returns True if the agent is now fully
        offline (status changed from online to offline)."""
        conns = self._presence.get(agent_id, set())
        conns.discard(ws)
        if not conns:
            self._presence.pop(agent_id, None)
            return True
        return False

    def is_online(self, agent_id: str) -> bool:
        return agent_id.lower() in self._presence and bool(self._presence[agent_id.lower()])

    def online_agent_ids(self) -> Set[str]:
        return set(self._presence.keys())

    async def broadcast_presence(self, agent_id: str, status: str) -> None:
        """Notify online friends of a presence change via their private rooms.

        Only rooms that are currently active will receive the update. Friends
        can always query the latest status via the REST API.
        """
        if self.social_graph is None:
            return
        friends = self.social_graph.list_friends(agent_id)
        now_ms = int(time.time() * 1000)
        for friend_id in friends:
            if friend_id == agent_id:
                continue
            room_id = private_room_id(agent_id, friend_id)
            room = self._rooms.get(room_id)
            if room is None:
                continue
            frame = {
                "type": "PRESENCE_UPDATE",
                "room_id": room_id,
                "room_type": "PRIVATE",
                "agent_id": agent_id,
                "status": status,
                "timestamp": now_ms,
            }
            await room.broadcast(None, frame)


gateway = ChatGateway(
    keystore=keystore,
    security=SecurityPipeline(),
    rooms=RoomResolver(),
    social_graph=social_graph,
)


async def _proposal_cleanup_task() -> None:
    """Background task that evicts stale pending proposals every 60 seconds."""
    from .api import _evict_stale_proposals

    while True:
        await asyncio.sleep(60)
        try:
            _evict_stale_proposals()
        except Exception:
            # Cleanup failures must not crash the gateway.
            pass


async def _turn_counter_cleanup_task() -> None:
    """Background task that prunes stale TurnCounter entries every 5 minutes."""
    while True:
        await asyncio.sleep(300)
        try:
            pruned = gateway.security.turns.prune()
            if pruned:
                logger.info("PerunChat gateway pruned %d stale turn-counter entries", pruned)
        except Exception:
            # Cleanup failures must not crash the gateway.
            pass


def _load_room_members() -> None:
    """Load persisted room memberships into the in-memory resolver on startup."""
    try:
        with db_module.db(config.db_path()) as conn:
            rows = conn.execute(
                "SELECT room_id, agent_id FROM room_members"
            ).fetchall()
            for row in rows:
                gateway.rooms.add_member(row["room_id"], row["agent_id"])
    except Exception:
        # Room ACLs are best-effort on startup; friendships will still work
        # because the social graph endpoint re-populates them on accept.
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.gateway = gateway
    _load_room_members()
    cleanup = asyncio.create_task(_proposal_cleanup_task())
    turn_cleanup = asyncio.create_task(_turn_counter_cleanup_task())
    yield
    cleanup.cancel()
    turn_cleanup.cancel()
    try:
        await cleanup
    except asyncio.CancelledError:
        pass
    try:
        await turn_cleanup
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="PerunChat Gateway",
    version="0.2.0",
    description="Runtime-agnostic federated agent-to-agent communication gateway.",
    lifespan=lifespan,
)
app.include_router(router)

# Serve the social-manager dashboard at /dashboard/social/.
_dashboard_dir = Path(__file__).resolve().parent / "dashboard"
if (_dashboard_dir / "social_manager.html").exists():
    app.mount(
        "/dashboard/social",
        StaticFiles(directory=str(_dashboard_dir), html=True),
        name="social_dashboard",
    )


def extract_bearer(authorization: str) -> Optional[str]:
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return None


def _bearer_from_query(ws: WebSocket) -> Optional[str]:
    """Browser WebSocket cannot set custom headers, so allow token via ?token=."""
    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(str(ws.url))
        values = parse_qs(parsed.query).get("token", [])
        return values[0] if values else None
    except Exception:
        return None


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "perunchat-gateway", "version": "0.2.0"}


@app.websocket("/v1/chat/ws")
async def handle_ws(
    ws: WebSocket,
    authorization: str = Header(default=""),
    x_agent_id: str = Header(default="", alias="X-Agent-ID"),
    x_agent_runtime: str = Header(default="custom", alias="X-Agent-Runtime"),
    x_agent_version: str = Header(default="0.0.0", alias="X-Agent-Version"),
):
    remote_addr = ws.client.host if ws.client else "unknown"

    token = extract_bearer(authorization) or _bearer_from_query(ws)
    key_record = None

    if token:
        # Brute-force protection: check auth-failure rate limiter before hashing.
        if not await gateway.security.check_auth_allowed(remote_addr):
            await ws.close(code=1008, reason="auth_rate_limited")
            return

        key_record = gateway.keystore.authenticate(token)
        if key_record is None:
            await gateway.security.record_auth_failure(remote_addr)
            await ws.close(code=1008, reason="invalid_or_revoked_key")
            return

        # The API key is bound to a single agent_id. Clients may not claim a different
        # identity, because agent_id is the unique chat participant name.
        if x_agent_id and x_agent_id.lower() != key_record.agent_id.lower():
            await gateway.security.record_auth_failure(remote_addr)
            await ws.close(code=1008, reason="mismatched_agent_id")
            return
    else:
        # Anonymous connections are allowed only as read-only observers in public
        # rooms. They cannot join group/private rooms or send frames.
        pass

    # All internal identifiers are lowercased so comparisons and room IDs are
    # case-insensitive while display names preserve original casing.
    agent_id = key_record.agent_id.lower() if key_record else None
    scopes = key_record.scopes if key_record else []
    runtime = x_agent_runtime or "custom" if key_record else "anonymous"
    version = x_agent_version or "0.0.0" if key_record else "0.0.0"
    is_anonymous = key_record is None

    await ws.accept()
    active_rooms: Dict[str, Room] = {}

    if agent_id:
        became_online = gateway.mark_online(agent_id, ws)
        if became_online:
            try:
                await gateway.broadcast_presence(agent_id, "online")
            except Exception:
                pass

    try:
        while True:
            raw = await ws.receive_text()
            try:
                envelope = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(
                    json.dumps({"type": "ERROR", "message": "invalid_json"})
                )
                continue

            msg_type = envelope.get("type")
            room_id = envelope.get("room_id")
            room_type = envelope.get("room_type", "GROUP")

            if msg_type == "ROOM_JOIN" and room_id:
                scope, _ = parse_room_id(room_id)
                if is_anonymous and scope != "pub":
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "ERROR",
                                "message": "room_access_denied",
                                "room_id": room_id,
                            }
                        )
                    )
                    continue

                # Enforce room ACLs: public rooms need a matching scope (or anonymous
                # read-only access); group/private rooms need explicit membership or
                # scope match. Private rooms also allow entry when the two participants
                # are confirmed friends.
                can_join = (
                    is_anonymous
                    and scope == "pub"
                ) or gateway.rooms.can_join(room_id, agent_id or "", scopes=scopes)
                if not can_join:
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "ERROR",
                                "message": "room_access_denied",
                                "room_id": room_id,
                            }
                        )
                    )
                    continue

                room = await gateway.ensure_room(room_id, room_type)
                await room.join(ws, agent_id or f"anon:{remote_addr}")
                active_rooms[room_id] = room
                if agent_id and scope == "pub":
                    # Persist public joins so GET /api/v1/rooms can offer a
                    # durable catalog of channels across gateway restarts.
                    _persist_room_member(room_id, agent_id)
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ROOM_JOINED",
                            "room_id": room_id,
                            "room_type": room_type,
                        }
                    )
                )
                await room.replay_history(ws, agent_id or "")
                continue

            if msg_type == "ROOM_LEAVE" and room_id:
                room = active_rooms.pop(room_id, None)
                if room:
                    await room.leave(ws)
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ROOM_LEFT",
                            "room_id": room_id,
                            "room_type": room_type,
                        }
                    )
                )
                continue

            # Anonymous observers are read-only.
            if is_anonymous:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ERROR",
                            "message": "anonymous_read_only",
                            "room_id": room_id,
                        }
                    )
                )
                continue

            target_room = active_rooms.get(room_id)
            if target_room is None:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ERROR",
                            "message": "not_joined",
                            "room_id": room_id,
                        }
                    )
                )
                continue

            # Inject sender identity server-side before validation so clients
            # (especially browser dashboards) don't need to know their bound agent_id.
            frame = envelope
            frame.setdefault("sender", {})
            frame["sender"]["agent_id"] = agent_id
            frame["sender"]["runtime"] = runtime
            frame["sender"]["version"] = version

            # Reject oversized payloads before any further processing.
            payload = frame.get("payload")
            payload_size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(json.dumps(payload).encode("utf-8"))
            if payload_size > _MAX_PAYLOAD_BYTES:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ERROR",
                            "message": "payload_too_large",
                            "max_bytes": _MAX_PAYLOAD_BYTES,
                        }
                    )
                )
                continue

            # Validate frame shape after server-side sender injection.
            try:
                ChatFrame.model_validate(frame)
            except Exception as exc:
                await ws.send_text(
                    json.dumps({"type": "ERROR", "message": "invalid_frame", "detail": str(exc)})
                )
                continue

            # Security pipeline checks.
            seq = envelope.get("sequence_number", 0)
            ts = envelope.get("timestamp", 0)
            thread_id = envelope.get("thread_id", "default")
            ok, reason = await gateway.security.check_ingress(
                agent_id=agent_id,
                room_id=room_id,
                sequence_number=seq,
                timestamp_ms=ts,
                thread_id=thread_id,
                runtime=runtime,
                nonce=envelope.get("id"),
            )
            if not ok:
                await ws.send_text(json.dumps({"type": "ERROR", "message": reason}))
                continue

            frame["turn_count"] = gateway.security.turns._counts.get(
                gateway.security.turns._key(thread_id, agent_id or ""), 0
            )

            # Egress DLP.
            safe_frame = gateway.security.scan_egress(frame)

            # If this is a proposal, validate any supplied approval token and stage
            # it for the dashboard.
            if safe_frame.get("content_type") == "application/perun-proposal":
                payload = safe_frame.get("payload") or {}
                proposal_id = payload.get("proposal_id")
                if proposal_id:
                    # A state-changing proposal must carry a valid approval token.
                    token = payload.get("approval_token")
                    execution_details = payload.get("execution_details") or {}
                    expiry_epoch = payload.get("approval_expiry_epoch")
                    nonce = payload.get("approval_nonce")
                    if not token or expiry_epoch is None or not nonce:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "proposal_missing_approval_token",
                                    "proposal_id": proposal_id,
                                }
                            )
                        )
                        continue
                    ok, hitl_reason = hitl_engine.validate(
                        token=token,
                        proposal_id=proposal_id,
                        execution_details=execution_details,
                        expiry_epoch=expiry_epoch,
                        nonce=nonce,
                    )
                    if not ok:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": f"proposal_approval_invalid:{hitl_reason}",
                                    "proposal_id": proposal_id,
                                }
                            )
                        )
                        continue
                    safe_frame["_approval_validated"] = True
                    safe_frame["_staged_at"] = int(time.time())
                    pending_proposals[proposal_id] = safe_frame

            await target_room.broadcast(ws, safe_frame)

    except WebSocketDisconnect:
        pass
    finally:
        for room in active_rooms.values():
            await room.leave(ws)
        if agent_id:
            became_offline = gateway.mark_offline(agent_id, ws)
            if became_offline:
                try:
                    await gateway.broadcast_presence(agent_id, "offline")
                except Exception:
                    pass


def main() -> None:
    import uvicorn

    uvicorn.run(
        "perunchat.gateway:app",
        host=config.api_bind_host(),
        port=config.api_bind_port(),
        reload=False,
    )


if __name__ == "__main__":
    main()
