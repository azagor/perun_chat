"""
PerunChat WebSocket Ingress Gateway

FastAPI/WebSocket entry point for agent chat frames. Integrates the security
pipeline (rate limiting, sequence validation, turn counters, DLP), key
authentication, and room broadcasting. Runtime-agnostic: any agent runtime that
sends the correct headers and frame schema can connect.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

from fastapi import Depends, FastAPI, Header, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("perunchat.gateway")

from . import config, db as db_module
from . import __version__
from . import api as api_module
from .api import (
    _persist_room_member,
    keystore,
    router,
    social_graph,
)
from .keys import KeyStore
from .models import ChatFrame
from .rooms import RoomResolver, parse_room_id, private_room_id, room_managed_by
from .security import SecurityPipeline


# Per-room message history cap and max payload size for backpressure.
_MAX_ROOM_HISTORY = 500
_MAX_PAYLOAD_BYTES = 8192
# Managed project rooms accept larger package-transfer frames so external agents
# can hand off ZIP work packages without splitting into thousands of chunks.
_PACKAGE_MAX_PAYLOAD_BYTES = 524_288  # 512 KiB
# Hard ceiling on the raw incoming WebSocket text frame before parsing.
# Measured in characters: receive_text() returns str. This is a cheap
# pre-parse guard; the WebSocket server also enforces its own byte limit.
MAX_FRAME_BYTES = _PACKAGE_MAX_PAYLOAD_BYTES + 16_384
# Per-socket send timeout so one stalled client cannot block the whole room.
_SEND_TIMEOUT = 5.0
_PACKAGE_CONTENT_TYPE = "application/perun-package"
_EXECUTION_CONTENT_TYPE = "application/perun-execution"
_EXECUTION_RESULT_CONTENT_TYPE = "application/perun-execution-result"
_EXECUTION_CONTENT_TYPES = {
    _EXECUTION_CONTENT_TYPE,
    _EXECUTION_RESULT_CONTENT_TYPE,
    _PACKAGE_CONTENT_TYPE,
}


def _is_managed_project_room(room_id: str) -> bool:
    """True only for group rooms explicitly managed in the gateway DB."""
    try:
        scope, _ = parse_room_id(room_id)
    except ValueError:
        return False
    if scope != "grp":
        return False
    return room_managed_by(room_id) is not None


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
        # History is stored as (seq, frame) tuples so replay can dedupe live
        # frames that arrive while the replay loop is yielding on the socket.
        self._history: deque[Tuple[int, Dict[str, Any]]] = deque(maxlen=_MAX_ROOM_HISTORY)
        self._seq = 0
        # Sockets still replaying history buffer live frames here instead of
        # receiving them out of order from the main broadcast loop.
        self._replaying: Set[WebSocket] = set()
        self._pending: Dict[WebSocket, list] = {}

    async def join(self, ws: WebSocket, agent_id: str) -> None:
        self.connections[ws] = agent_id
        self._replaying.add(ws)
        self._pending[ws] = []

    async def leave(self, ws: WebSocket) -> None:
        self.connections.pop(ws, None)
        self._replaying.discard(ws)
        self._pending.pop(ws, None)

    def online_agent_ids(self) -> Set[str]:
        """Return the set of authenticated agent IDs currently connected."""
        ids: Set[str] = set()
        for agent_id in self.connections.values():
            if agent_id and not agent_id.startswith("anon:"):
                ids.add(agent_id)
        return ids

    def _blocked(self, recipient_agent_id: str, sender_agent_id: str) -> bool:
        """Return True if delivery from sender to recipient is blocked."""
        graph = gateway.social_graph
        return bool(
            sender_agent_id
            and recipient_agent_id
            and graph is not None
            and graph.is_blocked(recipient_agent_id, sender_agent_id)
        )

    async def _send(self, conn: WebSocket, message: str) -> bool:
        """Send to one socket with a timeout; return False if it should be dropped."""
        try:
            await asyncio.wait_for(conn.send_text(message), _SEND_TIMEOUT)
            return True
        except Exception:
            return False

    async def broadcast(self, sender: WebSocket, frame: Dict[str, Any]) -> None:
        sender_agent_id = self.connections.get(sender)
        recipient_id = str(frame.get("recipient_id") or "").strip()
        directed = bool(recipient_id)
        message = json.dumps(frame, ensure_ascii=False)
        self._seq += 1
        seq = self._seq
        # Only store real chat frames for late-joiners; ephemeral control frames
        # such as PRESENCE_UPDATE should not be replayed. Directed execution
        # frames are never stored so another member cannot replay them later.
        # Store a deep copy so any later mutation of the broadcast frame cannot
        # corrupt the replay buffer, and preserve the original sender exactly.
        stored = bool(
            (frame.get("sender") or {}).get("agent_id")
            and frame.get("content_type") != _PACKAGE_CONTENT_TYPE
            and not directed
        )
        if stored:
            self._history.append((seq, copy.deepcopy(frame)))
        targets: list[WebSocket] = []
        for conn, recipient_agent_id in list(self.connections.items()):
            if directed and recipient_agent_id != recipient_id:
                continue
            if self._blocked(recipient_agent_id, sender_agent_id):
                continue
            if conn in self._replaying:
                # Buffer for sockets still receiving history so ordering is preserved.
                self._pending.setdefault(conn, []).append(
                    (seq, message, stored, sender_agent_id)
                )
            else:
                targets.append(conn)
        results = await asyncio.gather(*(self._send(c, message) for c in targets))
        for conn, ok in zip(targets, results):
            if not ok:
                await self.leave(conn)
                try:
                    await conn.close()
                except Exception:
                    pass

    async def replay_history(self, ws: WebSocket, agent_id: str) -> None:
        """Send the buffered recent history to a newly joined connection."""
        try:
            snap_seq = self._seq
            # Replay chronologically, not by broadcast sequence. A delayed
            # outbox retry can carry an older timestamp than a frame that was
            # broadcast first; clients that append history need the timestamp
            # order to match the chat log.
            snap = sorted(
                list(self._history),
                key=lambda item: int((item[1] or {}).get("timestamp") or 0),
            )
            for seq, frame in snap:
                if ws not in self.connections:
                    return
                sender_id = (frame.get("sender") or {}).get("agent_id")
                if self._blocked(agent_id, sender_id):
                    continue
                if not await self._send(ws, json.dumps(frame, ensure_ascii=False)):
                    await self.leave(ws)
                    return
            while True:
                pend = self._pending.get(ws)
                if pend is None or ws not in self.connections:
                    return
                # Drop stored frames that are already in the snapshot; ephemeral
                # frames such as presence updates are always delivered.
                batch = [b for b in pend if not (b[2] and b[0] <= snap_seq)]
                pend.clear()
                if not batch:
                    self._replaying.discard(ws)
                    return
                for seq, message, stored, sender_id in batch:
                    if self._blocked(agent_id, sender_id):
                        continue
                    if not await self._send(ws, message):
                        await self.leave(ws)
                        return
        finally:
            self._replaying.discard(ws)
            self._pending.pop(ws, None)


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
    turn_cleanup = asyncio.create_task(_turn_counter_cleanup_task())
    yield
    turn_cleanup.cancel()
    try:
        await turn_cleanup
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="PerunChat Gateway",
    version="0.2.5",
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


def _effective_runtime(key_record: Optional[Any], header_runtime: str) -> str:
    """Return the authenticated runtime for this connection.

    The API key's ``supported_runtime`` is authoritative for browser clients,
    which cannot set custom WebSocket headers. Explicit non-default headers
    from autonomous clients are still honored so existing runtimes continue to
    work, but the key record prevents arbitrary runtime spoofing.
    """
    if key_record is None:
        return "anonymous"
    key_runtime = str(key_record.supported_runtime or "")
    if key_runtime and key_runtime != "custom":
        logger.debug(
            "Using key runtime %r for agent %s (header was %r)",
            key_runtime, key_record.agent_id, header_runtime,
        )
        return key_runtime
    # Generic/custom key: let the caller declare a more specific runtime.
    runtime = header_runtime or "custom"
    logger.debug(
        "Using header/runtime %r for agent %s (key runtime is generic)",
        runtime, key_record.agent_id,
    )
    return runtime


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "perunchat-gateway", "version": __version__}


@app.websocket("/v1/chat/ws")
async def handle_ws(
    ws: WebSocket,
    authorization: str = Header(default=""),
    x_agent_id: str = Header(default="", alias="X-Agent-ID"),
    x_agent_runtime: str = Header(default="custom", alias="X-Agent-Runtime"),
    x_agent_version: str = Header(default="0.0.0", alias="X-Agent-Version"),
    x_agent_stream: str = Header(default="", alias="X-Agent-Stream"),
):
    remote_addr = ws.client.host if ws.client else "unknown"
    x_agent_stream = x_agent_stream.strip()
    if len(x_agent_stream) > 64:
        await ws.close(code=1008, reason="invalid_stream_id")
        return

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
    runtime = _effective_runtime(key_record, x_agent_runtime)
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
            if len(raw) > MAX_FRAME_BYTES:
                await ws.close(code=1009, reason="frame_too_large")
                break

            try:
                envelope = json.loads(raw)
            except (json.JSONDecodeError, RecursionError):
                await ws.send_text(
                    json.dumps({"type": "ERROR", "message": "invalid_json"})
                )
                continue

            if not isinstance(envelope, dict):
                await ws.send_text(
                    json.dumps({"type": "ERROR", "message": "invalid_envelope"})
                )
                continue

            msg_type = envelope.get("type")
            room_id = envelope.get("room_id")
            room_type = envelope.get("room_type", "GROUP")

            # room_id is used as a dict key before parse_room_id is called, so a
            # non-string value must be rejected here rather than later.
            if room_id is not None and not isinstance(room_id, str):
                await ws.send_text(
                    json.dumps(
                        {"type": "ERROR", "message": "invalid_envelope_fields"}
                    )
                )
                continue

            seq = envelope.get("sequence_number", 0)
            ts = envelope.get("timestamp", 0)
            thread_id = envelope.get("thread_id", "default")
            if (
                not isinstance(seq, int)
                or isinstance(seq, bool)
                or seq < 0
                or not isinstance(ts, (int, float))
                or isinstance(ts, bool)
                or not isinstance(thread_id, str)
                or isinstance(room_type, bool)
                or not isinstance(room_type, str)
            ):
                await ws.send_text(
                    json.dumps(
                        {"type": "ERROR", "message": "invalid_envelope_fields"}
                    )
                )
                continue

            if msg_type == "ROOM_JOIN" and room_id:
                try:
                    scope, _ = parse_room_id(room_id)
                except ValueError:
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "ERROR",
                                "message": "invalid_room_id",
                                "room_id": room_id,
                            }
                        )
                    )
                    continue
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
                try:
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
                except BaseException:
                    await room.leave(ws)
                    active_rooms.pop(room_id, None)
                    raise
                continue

            if msg_type == "ROOM_LEAVE" and room_id:
                room = active_rooms.pop(room_id, None)
                if room:
                    await room.leave(ws)
                # Public room memberships are not durable: leaving means leaving.
                # Remove the persisted catalog entry and the in-memory ACL so the
                # agent no longer appears as a member of the channel.
                if agent_id and room_id.startswith("pub:"):
                    try:
                        from .api import _remove_room_member
                        _remove_room_member(room_id, agent_id)
                        acls = gateway.rooms._acls.get(room_id)
                        if acls:
                            acls.discard(agent_id.lower())
                    except Exception:
                        logger.exception(
                            "Failed to remove public room membership on leave: %s %s",
                            room_id,
                            agent_id,
                        )
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

            # Validate and coerce the sender envelope before touching nested fields.
            sender = envelope.get("sender")
            if sender is not None and not isinstance(sender, dict):
                await ws.send_text(
                    json.dumps(
                        {"type": "ERROR", "message": "invalid_envelope_fields"}
                    )
                )
                continue

            # Inject sender identity server-side before validation so clients
            # (especially browser dashboards) don't need to know their bound agent_id.
            # Dashboards mark manual operator messages with a runtime containing
            # "operator" (conventionally "<agent_id>-operator"). Preserve that
            # signal only for keys that are already registered as operator-class
            # runtimes, so arbitrary authenticated clients cannot disable turn limits
            # by claiming operator status.
            client_runtime = str((sender or {}).get("runtime") or "").lower()
            key_runtime = (
                str(key_record.supported_runtime or "").lower()
                if key_record
                else ""
            )
            key_is_operator_class = (
                key_runtime.endswith("-operator")
                or key_runtime in {"operator", "perun", "dashboard", "human"}
            )
            if (
                key_is_operator_class
                and (client_runtime.endswith("-operator") or client_runtime == "operator")
            ):
                frame_runtime = f"{agent_id}-operator"
            else:
                frame_runtime = runtime
            frame = envelope
            frame["sender"] = {
                "agent_id": agent_id,
                "runtime": frame_runtime,
                "version": version,
            }

            content_type = frame.get("content_type")
            is_execution_frame = content_type in _EXECUTION_CONTENT_TYPES
            is_managed_room = _is_managed_project_room(room_id)

            # Execution/package frames are only allowed in managed project chats.
            if is_execution_frame and not is_managed_room:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ERROR",
                            "message": "package_frames_project_rooms_only",
                            "room_id": room_id,
                        }
                    )
                )
                continue

            if is_managed_room:
                owner_id = room_managed_by(room_id) or ""
                recipient_id = str(frame.get("recipient_id") or "").strip()
                payload_obj = frame.get("payload") or {}
                if content_type == _EXECUTION_RESULT_CONTENT_TYPE:
                    if not owner_id or agent_id.lower() != owner_id.lower():
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "execution_result_owner_only",
                                    "room_id": room_id,
                                }
                            )
                        )
                        continue
                    if not recipient_id:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "execution_result_recipient_required",
                                    "room_id": room_id,
                                }
                            )
                        )
                        continue
                elif content_type == _EXECUTION_CONTENT_TYPE:
                    if payload_obj.get("op") == "package_ready":
                        if not owner_id or agent_id.lower() != owner_id.lower():
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "ERROR",
                                        "message": "package_ready_owner_only",
                                        "room_id": room_id,
                                    }
                                )
                            )
                            continue
                        if not recipient_id:
                            await ws.send_text(
                                json.dumps(
                                    {
                                        "type": "ERROR",
                                        "message": "package_ready_recipient_required",
                                        "room_id": room_id,
                                    }
                                )
                            )
                            continue
                elif content_type == _PACKAGE_CONTENT_TYPE:
                    if not recipient_id:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "package_recipient_required",
                                    "room_id": room_id,
                                }
                            )
                        )
                        continue
                    if (
                        owner_id
                        and agent_id.lower() != owner_id.lower()
                        and recipient_id.lower() != owner_id.lower()
                    ):
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "package_recipient_must_be_owner",
                                    "room_id": room_id,
                                }
                            )
                        )
                        continue

            # Reject oversized payloads before any further processing.
            payload = frame.get("payload")
            payload_size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(json.dumps(payload).encode("utf-8"))
            size_limit = (
                _PACKAGE_MAX_PAYLOAD_BYTES
                if content_type == _PACKAGE_CONTENT_TYPE and is_managed_room
                else _MAX_PAYLOAD_BYTES
            )
            if payload_size > size_limit:
                await ws.send_text(
                    json.dumps(
                        {
                            "type": "ERROR",
                            "message": "payload_too_large",
                            "max_bytes": size_limit,
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

            # Rate/sequence/timestamp validation. This intentionally does not
            # consume a turn credit yet; credits are committed only for frames
            # that survive proposal staging/validation and are about to broadcast.
            ok, reason = await gateway.security.pre_validate(
                agent_id=agent_id,
                room_id=room_id,
                sequence_number=seq,
                timestamp_ms=ts,
                thread_id=thread_id,
                runtime=frame_runtime,
                nonce=envelope.get("id"),
                content_type=content_type or "",
                stream_id=x_agent_stream,
            )
            if not ok:
                await ws.send_text(json.dumps({"type": "ERROR", "message": reason}))
                continue

            # Egress DLP. Package frames carry binary base64 that the entropy
            # scanner would corrupt, so they bypass DLP inside managed rooms.
            if content_type == _PACKAGE_CONTENT_TYPE and is_managed_room:
                safe_frame = frame
            else:
                safe_frame = gateway.security.scan_egress(frame)

            # Strip internal state keys that clients must never set themselves.
            for _internal_key in ("_approval_validated", "_staged_at"):
                safe_frame.pop(_internal_key, None)

            # If this is a proposal, either stage it (token-less) or validate the
            # supplied approval token. Rejections here must not consume turn credits.
            if safe_frame.get("content_type") == "application/perun-proposal":
                payload = safe_frame.get("payload")
                if not isinstance(payload, dict):
                    await ws.send_text(
                        json.dumps(
                            {"type": "ERROR", "message": "invalid_proposal_payload"}
                        )
                    )
                    continue
                proposal_id = payload.get("proposal_id")
                if not isinstance(proposal_id, str) or not proposal_id:
                    await ws.send_text(
                        json.dumps(
                            {"type": "ERROR", "message": "invalid_proposal_payload"}
                        )
                    )
                    continue

                token = payload.get("approval_token")
                execution_details = payload.get("execution_details") or {}
                expiry_epoch = payload.get("approval_expiry_epoch")
                approval_nonce = payload.get("approval_nonce")
                token_fields_present = (
                    token is not None
                    or expiry_epoch is not None
                    or approval_nonce is not None
                )
                if not token_fields_present:
                    # Token-less proposals are staged for dashboard approval.
                    if api_module.proposal_store.get(proposal_id) is not None:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "proposal_id_conflict",
                                    "proposal_id": proposal_id,
                                }
                            )
                        )
                        continue
                    safe_frame["_staged_at"] = int(time.time())
                    try:
                        api_module.proposal_store.stage(safe_frame)
                    except api_module.ProposalStoreFull:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "ERROR",
                                    "message": "proposal_store_full",
                                    "proposal_id": proposal_id,
                                    "room_id": room_id,
                                }
                            )
                        )
                        continue
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "PROPOSAL_STAGED",
                                "proposal_id": proposal_id,
                                "room_id": room_id,
                            }
                        )
                    )
                    continue

                # Harden the call into the HITL engine: coerce types so that a
                # malformed token cannot raise inside compare_digest or expiry math.
                # If any token field is present, all must be present and well-formed.
                if not (
                    isinstance(token, str)
                    and token.isascii()
                    and isinstance(approval_nonce, str)
                    and isinstance(expiry_epoch, (int, float))
                    and not isinstance(expiry_epoch, bool)
                    and isinstance(execution_details, dict)
                ):
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "ERROR",
                                "message": "proposal_approval_invalid:malformed",
                                "proposal_id": proposal_id,
                            }
                        )
                    )
                    continue

                ok, hitl_reason = api_module.hitl_engine.validate(
                    token=token,
                    proposal_id=proposal_id,
                    execution_details=execution_details,
                    expiry_epoch=expiry_epoch,
                    nonce=approval_nonce,
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
                # Remove from the staging store immediately after validation so
                # a token consumption is not left dangling if broadcast fails.
                api_module.proposal_store.remove(proposal_id)

            # Commit the turn only for frames that will actually be broadcast.
            ok, reason = gateway.security.commit_turn(
                agent_id=agent_id,
                room_id=room_id,
                runtime=frame_runtime,
                content_type=content_type or "",
            )
            if not ok:
                await ws.send_text(json.dumps({"type": "ERROR", "message": reason}))
                continue

            try:
                turn_key = gateway.security.turns._key(room_id, agent_id or "")
                safe_frame["turn_count"] = int(
                    gateway.security.turns._credits.get(turn_key, gateway.security.turns.MAX_CREDITS)
                )
            except Exception:
                safe_frame["turn_count"] = 0

            # For rooms still replaying to this socket, route the frame through the
            # pending queue so history stays in order.
            if (
                target_room is not None
                and ws in target_room._replaying
                and ws in target_room._pending
            ):
                target_room._pending.setdefault(ws, []).append(
                    (
                        target_room._seq,
                        json.dumps(safe_frame, ensure_ascii=False),
                        bool(safe_frame.get("sender", {}).get("agent_id")),
                        agent_id,
                    )
                )
                continue

            await target_room.broadcast(ws, safe_frame)

    except WebSocketDisconnect:
        pass
    finally:
        for room_id, room in active_rooms.items():
            await room.leave(ws)
            # Clean up public room memberships on disconnect, just as on explicit
            # ROOM_LEAVE, so a dropped connection does not leave the agent in the
            # channel catalog forever.
            if agent_id and room_id.startswith("pub:"):
                try:
                    from .api import _remove_room_member
                    _remove_room_member(room_id, agent_id)
                    acls = gateway.rooms._acls.get(room_id)
                    if acls:
                        acls.discard(agent_id.lower())
                except Exception:
                    logger.exception(
                        "Failed to remove public room membership on disconnect: %s %s",
                        room_id,
                        agent_id,
                    )
        if agent_id:
            became_offline = gateway.mark_offline(agent_id, ws)
            if became_offline:
                try:
                    await gateway.broadcast_presence(agent_id, "offline")
                except Exception:
                    pass


def main() -> None:
    import uvicorn

    # Enforce the same raw-frame cap at the WebSocket transport layer as a
    # defense-in-depth guard. The receive-loop check already rejects oversized
    # text, but setting ws_max_size closes the frame at the server before the
    # application even sees it.
    uvicorn.run(
        "perunchat.gateway:app",
        host=config.api_bind_host(),
        port=config.api_bind_port(),
        reload=False,
        ws_max_size=MAX_FRAME_BYTES,
    )


if __name__ == "__main__":
    main()
