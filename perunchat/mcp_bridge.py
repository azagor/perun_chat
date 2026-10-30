"""
Model Context Protocol (MCP) Bridge

Provides a functional MCP server wrapper for PerunChat:
  - Resource: perunchat://rooms/{room_id}
  - Tool: perunchat_send_message(room_id, room_type, text, thread_id)
  - Tool: perunchat_join_room(room_id, room_type)
  - Tool: perunchat_leave_room(room_id, room_type)
  - Tool: perunchat_propose_action(room_id, risk_tier, action_domain, description, details, justification)
  - Tools: perunchat_claim_execution, perunchat_request_download_package,
    perunchat_upload_package, perunchat_write_target_file,
    perunchat_execution_status, perunchat_revoke_execution_claim
    (Perun shared-project external execution handoff)

This bridge uses the official `mcp` package when available; if not, it falls back
to a minimal JSON-RPC-ish stub so import-time dependencies stay light.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

logger = logging.getLogger("perunchat.mcp_bridge")

# Perun proposal ids are ``prop_`` + 12 lowercase hex characters. Validate at
# the tool boundary so malformed ids cannot allocate lock entries or reach the
# wire; empty ids stay allowed for ops that do not use them (transfer_status).
_PROPOSAL_ID_RE = re.compile(r"prop_[0-9a-f]{12}")

try:
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    _HAS_MCP = True
except Exception:  # pragma: no cover
    _HAS_MCP = False


class PerunChatMCPServer:
    """
    MCP server for PerunChat.

    Manages a persistent async PerunChatClient and exposes PerunChat as MCP
    resources and tools. Tool calls are async and run in the MCP server's event
    loop.
    """

    # Perun's external handoff protocol (see perun/instructions/perun-external-execution.md).
    _EXECUTION_CONTENT_TYPE = "application/perun-execution"
    _EXECUTION_RESULT_CONTENT_TYPE = "application/perun-execution-result"
    _PACKAGE_CONTENT_TYPE = "application/perun-package"
    _DEFAULT_CHUNK_BYTES = 256 * 1024
    _MAX_CHUNK_BYTES = 256 * 1024
    _MAX_PACKAGE_BYTES = 64 * 1024 * 1024
    _MAX_INLINE_FILE_BYTES = 10 * 1024 * 1024
    _MAX_CONTROL_FRAMES = 512
    _MAX_PACKAGE_TRANSFERS = 8
    _CHUNK_SEND_DELAY = 0.03
    _PACKAGE_RESULT_TIMEOUT = 180.0
    _MAX_PROPOSAL_LOCKS = 256

    def __init__(self, endpoint: str, api_key: str, agent_id: str) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.agent_id = agent_id
        self._client: Any = None
        self._RoomType: Any = None
        self._RiskTier: Any = None
        self._perun_agent_ids: Dict[str, str] = {}
        self._proposal_locks: Dict[str, asyncio.Lock] = {}
        self._last_errors: Dict[str, str] = {}
        self._room_messages: Dict[str, List[Dict[str, Any]]] = {}
        # Control/result frames and package chunks are kept separately so the
        # chat resource stays small and chunks do not evict recent messages.
        self._control_frames: List[Dict[str, Any]] = []
        self._control_seq = 0
        self._control_trim_warned = False
        self._package_chunks: Dict[str, Dict[int, bytes]] = {}
        self._package_bytes: Dict[str, int] = {}
        self._package_total_bytes = 0
        self._package_errors: Dict[str, str] = {}
        # Only buffer chunks for transfers Perun announced via package_ready.
        self._expected_downloads: Dict[str, float] = {}
        self._expected_chunks: Dict[str, int] = {}

    async def _ensure_client(self) -> Any:
        """Lazy import and connect the async Python SDK."""
        if self._client is not None:
            # Reconnect if the socket was closed.
            ws = getattr(self._client, "ws", None)
            if ws is None or getattr(ws, "close_code", None) is not None:
                await self._client.connect()
            return self._client
        try:
            from perunchat.client import PerunChatClient, RiskTier, RoomType
        except Exception as exc:  # pragma: no cover
            raise ImportError(
                "PerunChat MCP bridge requires the perunchat package. "
                "Install with: pip install perunchat"
            ) from exc
        self._client = PerunChatClient(
            endpoint=self.endpoint,
            api_key=self.api_key,
            agent_id=self.agent_id,
            runtime="mcp",
            filter_self_echo=True,
        )
        self._RoomType = RoomType
        self._RiskTier = RiskTier

        self._client.on_message(self._on_frame)
        await self._client.connect()
        return self._client

    def _on_frame(self, frame: Dict[str, Any]) -> None:
        if frame.get("type") == "ERROR":
            room_id = str(frame.get("room_id") or "")
            message = str(frame.get("message") or "gateway error")
            self._last_errors[room_id] = message
            logger.warning("PerunChat gateway error: %s (room=%s)", message, room_id)
            return
        content_type = frame.get("content_type")
        if content_type == self._PACKAGE_CONTENT_TYPE:
            self._buffer_package_frame(frame)
            return
        if content_type in (
            self._EXECUTION_CONTENT_TYPE,
            self._EXECUTION_RESULT_CONTENT_TYPE,
        ):
            payload = frame.get("payload") or {}
            sender_id = str((frame.get("sender") or {}).get("agent_id") or "").strip()
            if sender_id and (
                content_type == self._EXECUTION_RESULT_CONTENT_TYPE
                or payload.get("op") == "package_ready"
            ):
                # The gateway only permits these from the managed-room owner.
                room_key = str(frame.get("room_id") or "").strip()
                if room_key:
                    self._perun_agent_ids[room_key] = sender_id
            if (
                content_type == self._EXECUTION_CONTENT_TYPE
                and payload.get("op") == "package_ready"
                and payload.get("transfer_id")
            ):
                transfer_id = str(payload["transfer_id"])
                self._expected_downloads[transfer_id] = time.time()
                try:
                    self._expected_chunks[transfer_id] = int(
                        payload.get("total_chunks") or 0
                    )
                except (TypeError, ValueError):
                    self._expected_chunks[transfer_id] = 0
                if len(self._expected_downloads) > self._MAX_PACKAGE_TRANSFERS:
                    oldest = min(
                        self._expected_downloads,
                        key=self._expected_downloads.get,
                    )
                    self._expected_downloads.pop(oldest, None)
                    self._expected_chunks.pop(oldest, None)
                    self._package_errors[oldest] = (
                        "evicted: too many concurrent transfers"
                    )
            self._control_seq += 1
            stored = dict(frame)
            stored["_mcp_seq"] = self._control_seq
            self._control_frames.append(stored)
            if len(self._control_frames) > self._MAX_CONTROL_FRAMES:
                overflow = len(self._control_frames) - self._MAX_CONTROL_FRAMES
                if not self._control_trim_warned:
                    logger.warning(
                        "control frame queue full, dropping %d oldest frame(s)",
                        overflow,
                    )
                    self._control_trim_warned = True
                del self._control_frames[:overflow]
            else:
                self._control_trim_warned = False
            return
        room_id = frame.get("room_id")
        if room_id:
            self._room_messages.setdefault(room_id, []).append(frame)
            # Bound memory per room.
            self._room_messages[room_id] = self._room_messages[room_id][-500:]

    def _drop_package(self, transfer_id: str) -> None:
        self._package_chunks.pop(transfer_id, None)
        self._package_total_bytes -= self._package_bytes.pop(transfer_id, 0)
        self._package_errors.pop(transfer_id, None)
        self._expected_downloads.pop(transfer_id, None)
        self._expected_chunks.pop(transfer_id, None)
        self._package_total_bytes = max(0, self._package_total_bytes)

    def _buffer_package_frame(self, frame: Dict[str, Any]) -> None:
        payload = frame.get("payload") or {}
        transfer_id = str(payload.get("transfer_id") or "")
        seq = payload.get("seq")
        data = payload.get("data")
        if not transfer_id or not isinstance(seq, int) or isinstance(seq, bool):
            return
        if not isinstance(data, str):
            return
        # Chunks are only accepted from the managed-room owner (Perun) and only
        # after its package_ready for this transfer. This stops another room
        # member from filling the download buffer with spoofed chunks.
        room_id = str(frame.get("room_id") or "").strip()
        sender = str((frame.get("sender") or {}).get("agent_id") or "").strip().lower()
        owner = self._perun_agent_ids.get(room_id, "").strip().lower()
        if owner and sender != owner:
            return
        if transfer_id not in self._expected_downloads:
            return
        total_chunks = self._expected_chunks.get(transfer_id, 0)
        if total_chunks and not 0 <= seq < total_chunks:
            self._package_errors[transfer_id] = (
                f"chunk {seq} is outside 0..{total_chunks - 1}"
            )
            return
        if (
            transfer_id not in self._package_chunks
            and len(self._package_chunks) >= self._MAX_PACKAGE_TRANSFERS
        ):
            evicted = next(iter(self._package_chunks))
            self._drop_package(evicted)
            self._package_errors[evicted] = (
                "evicted: too many concurrent transfers"
            )
        chunks = self._package_chunks.setdefault(transfer_id, {})
        try:
            chunk = base64.b64decode(data, validate=True)
        except Exception:
            self._package_errors[transfer_id] = f"chunk {seq} is not valid base64"
            return
        if seq in chunks:
            if chunks[seq] != chunk:
                self._package_errors[transfer_id] = (
                    f"duplicate chunk {seq} with different data"
                )
            return
        if self._package_total_bytes + len(chunk) > self._MAX_PACKAGE_BYTES:
            self._package_errors[transfer_id] = "download exceeds the local package size cap"
            return
        self._package_bytes[transfer_id] = (
            self._package_bytes.get(transfer_id, 0) + len(chunk)
        )
        self._package_total_bytes += len(chunk)
        chunks[seq] = chunk

    def _handoff_root(self) -> Path:
        """Local directory the agent may use for downloaded/uploaded packages."""
        configured = os.environ.get("PERUNCHAT_HANDOFF_DIR")
        root = (
            Path(configured).expanduser()
            if configured
            else Path.cwd() / ".perunchat" / "handoff"
        )
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolve_handoff_path(self, raw: str, default_name: str) -> Path:
        root = self._handoff_root()
        if raw:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
        else:
            candidate = root / default_name
        candidate = candidate.resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"handoff path must be inside {root}")
        return candidate

    def _proposal_lock_for(self, proposal_id: str) -> asyncio.Lock:
        """Serialize handoff operations that target the same proposal.

        The table is bounded: above ``_MAX_PROPOSAL_LOCKS`` unlocked entries are
        evicted. An in-use lock is never dropped, so serialization is preserved.
        """
        lock = self._proposal_locks.get(proposal_id)
        if lock is None:
            if len(self._proposal_locks) >= self._MAX_PROPOSAL_LOCKS:
                for key, old in list(self._proposal_locks.items()):
                    if not old.locked():
                        del self._proposal_locks[key]
            lock = asyncio.Lock()
            self._proposal_locks[proposal_id] = lock
        return lock

    async def _join_room(self, client: Any, room_id: str, room_type: Any) -> None:
        joined = getattr(client, "_joined_rooms", None)
        if not isinstance(joined, set) or room_id not in joined:
            await client.join_room(room_id, room_type)

    async def _send_execution(
        self,
        client: Any,
        room_id: str,
        room_type: Any,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        await self._join_room(client, room_id, room_type)
        return await client.send_frame(
            room_id,
            room_type,
            self._EXECUTION_CONTENT_TYPE,
            payload,
            recipient_id=self._perun_agent_ids.get(room_id) or None,
        )

    async def _wait_for_frame(
        self, predicate, timeout: float, *, since: int = 0, room_id: str = ""
    ) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.1)
        while True:
            if room_id:
                error = self._last_errors.pop(room_id, "")
                if error:
                    raise RuntimeError(
                        f"PerunChat gateway rejected the frame: {error}"
                    )
            for frame in list(self._control_frames):
                if int(frame.get("_mcp_seq") or 0) <= since:
                    continue
                if predicate(frame):
                    try:
                        self._control_frames.remove(frame)
                    except ValueError:
                        pass
                    return frame
            if loop.time() >= deadline:
                raise TimeoutError("timed out waiting for Perun's execution response")
            await asyncio.sleep(0.05)

    async def _wait_for_result(
        self,
        op: str,
        proposal_id: str,
        timeout: float = 30.0,
        *,
        since: int = 0,
        transfer_id: str = "",
        room_id: str = "",
    ) -> Dict[str, Any]:
        def matches(frame: Dict[str, Any]) -> bool:
            if frame.get("content_type") != self._EXECUTION_RESULT_CONTENT_TYPE:
                return False
            payload = frame.get("payload") or {}
            if payload.get("op") not in (None, op):
                return False
            if payload.get("proposal_id") not in (None, proposal_id):
                return False
            if transfer_id and payload.get("transfer_id") not in (None, transfer_id):
                return False
            return True

        frame = await self._wait_for_frame(
            matches, timeout, since=since, room_id=room_id
        )
        return frame.get("payload") or {}

    async def _wait_for_package_ready(
        self,
        proposal_id: str,
        timeout: float = 60.0,
        *,
        since: int = 0,
        room_id: str = "",
    ) -> Dict[str, Any]:
        def matches(frame: Dict[str, Any]) -> bool:
            if frame.get("content_type") != self._EXECUTION_CONTENT_TYPE:
                return False
            payload = frame.get("payload") or {}
            return (
                payload.get("op") == "package_ready"
                and payload.get("proposal_id") == proposal_id
            )

        frame = await self._wait_for_frame(
            matches, timeout, since=since, room_id=room_id
        )
        return frame.get("payload") or {}

    async def _collect_package(
        self, transfer_id: str, total_chunks: int, timeout: float = 120.0
    ) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.1)
        while True:
            error = self._package_errors.get(transfer_id)
            if error:
                raise ValueError(error)
            chunks = self._package_chunks.get(transfer_id, {})
            if len(chunks) >= total_chunks and all(
                seq in chunks for seq in range(total_chunks)
            ):
                return b"".join(chunks[seq] for seq in range(total_chunks))
            if loop.time() >= deadline:
                received = len(chunks)
                raise TimeoutError(
                    f"timed out receiving package {transfer_id} "
                    f"({received}/{total_chunks} chunks)"
                )
            await asyncio.sleep(0.05)

    async def _upload_archive(
        self,
        client: Any,
        room_id: str,
        room_type: Any,
        proposal_id: str,
        archive: bytes,
        chunk_bytes: int = _DEFAULT_CHUNK_BYTES,
    ) -> Dict[str, Any]:
        total_size = len(archive)
        if total_size <= 0:
            raise ValueError("package is empty")
        if total_size > self._MAX_PACKAGE_BYTES:
            raise ValueError(
                f"package is {total_size} bytes; local cap is {self._MAX_PACKAGE_BYTES}"
            )
        chunk_bytes = max(16 * 1024, min(int(chunk_bytes), self._MAX_CHUNK_BYTES))
        total_chunks = (total_size + chunk_bytes - 1) // chunk_bytes
        transfer_id = f"trf_{uuid.uuid4().hex[:16]}"
        digest = hashlib.sha256(archive).hexdigest()

        await self._join_room(client, room_id, room_type)
        since = self._control_seq
        begin = await client.send_frame(
            room_id,
            room_type,
            self._EXECUTION_CONTENT_TYPE,
            {
                "op": "package_begin",
                "proposal_id": proposal_id,
                "transfer_id": transfer_id,
                "sha256": digest,
                "total_size": total_size,
                "total_chunks": total_chunks,
            },
            recipient_id=self._perun_agent_ids.get(room_id) or None,
        )
        _ = begin
        begin_result = await self._wait_for_result(
            "package_begin",
            proposal_id,
            timeout=30.0,
            since=since,
            transfer_id=transfer_id,
            room_id=room_id,
        )
        if not begin_result.get("ok", True):
            raise ValueError(
                str(begin_result.get("error") or "package_begin was rejected")
            )

        for seq in range(total_chunks):
            chunk = archive[seq * chunk_bytes : (seq + 1) * chunk_bytes]
            await client.send_frame(
                room_id,
                room_type,
                self._PACKAGE_CONTENT_TYPE,
                {
                    "transfer_id": transfer_id,
                    "seq": seq,
                    "total_chunks": total_chunks,
                    "data": base64.b64encode(chunk).decode("ascii"),
                },
                recipient_id=self._perun_agent_ids.get(room_id) or None,
            )
            if self._CHUNK_SEND_DELAY:
                await asyncio.sleep(self._CHUNK_SEND_DELAY)

        final_since = self._control_seq
        await client.send_frame(
            room_id,
            room_type,
            self._EXECUTION_CONTENT_TYPE,
            {"op": "package_end", "transfer_id": transfer_id},
            recipient_id=self._perun_agent_ids.get(room_id) or None,
        )

        def matches(frame: Dict[str, Any]) -> bool:
            if frame.get("content_type") != self._EXECUTION_RESULT_CONTENT_TYPE:
                return False
            payload = frame.get("payload") or {}
            op = payload.get("op")
            if payload.get("transfer_id") == transfer_id:
                return op in ("package_accepted", "package_rejected") or (
                    op == "package_end" and payload.get("ok") is False
                )
            # Lenient rejection frames may omit transfer_id. Same-proposal
            # handoffs are serialized, so proposal_id is unambiguous here.
            return (
                op == "package_rejected"
                and payload.get("transfer_id") in (None, "")
                and payload.get("proposal_id") == proposal_id
            )

        frame = await self._wait_for_frame(
            matches, self._PACKAGE_RESULT_TIMEOUT, since=final_since, room_id=room_id
        )
        result = frame.get("payload") or {}
        result["transfer_id"] = transfer_id
        if result.get("op") == "package_accepted":
            # Older Perun builds omitted ``ok`` from the success frame; treat a
            # matching package_accepted as success unless it explicitly failed.
            result.setdefault("ok", True)
        if result.get("op") == "package_rejected" or result.get("ok") is False:
            raise RuntimeError(
                f"upload failed: "
                f"{result.get('error') or result.get('op') or 'unknown error'}"
            )
        return result

    @staticmethod
    def _enum_member(
        enum_cls: Any, value: Any, field: str, *, default: str | None = None
    ) -> Any:
        """Resolve a strict enum member, rejecting typos and non-members.

        Silent fallbacks here are dangerous: an unknown ``risk_tier`` would be
        downgraded to TIER_2 and an unknown ``room_type`` would land the frame
        in a GROUP room. The caller must see the error.
        """
        if value is None and default is not None:
            value = default
        if not isinstance(value, str) or value not in enum_cls.__members__:
            allowed = ", ".join(enum_cls.__members__)
            raise ValueError(f"{field} must be one of: {allowed}")
        return enum_cls[value]

    @staticmethod
    def _string_argument(arguments: Dict[str, Any], key: str) -> str:
        """Return a required, non-empty string argument without coercing."""
        value = arguments.get(key)
        if value is None:
            raise ValueError(f"{key} is required")
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        value = value.strip()
        if not value:
            raise ValueError(f"{key} is required")
        return value

    def _proposal_id_argument(self, arguments: Dict[str, Any]) -> str:
        """Return a validated Perun proposal id."""
        value = self._string_argument(arguments, "proposal_id")
        if not _PROPOSAL_ID_RE.fullmatch(value):
            raise ValueError("proposal_id must match prop_<12 hex>")
        return value

    def _chunk_bytes_arg(self, value: Any) -> int:
        """Validate ``chunk_bytes``; ``_upload_archive`` clamps the range."""
        if value is None:
            return self._DEFAULT_CHUNK_BYTES
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("chunk_bytes must be an integer")
        return value

    def _build_single_file_archive(
        self, path: str, content: str, encoding: str
    ) -> bytes:
        normalized = str(path).replace("\\", "/").strip()
        pure = PurePosixPath(normalized)
        if not normalized or pure.is_absolute() or ".." in pure.parts:
            raise ValueError("path must be a project-relative path")
        if ":" in pure.parts[0]:
            raise ValueError("path must be a project-relative path without a drive prefix")
        lowered = normalized.lower()
        sensitive_prefixes = (
            ".git/",
            ".perun/",
            ".ssh/",
            ".aws/",
            ".gnupg/",
        )
        if lowered.startswith(sensitive_prefixes) or lowered in {
            ".env",
            "opencode.json",
            "perun.md",
        }:
            raise ValueError("path is sensitive and cannot be handed off")
        enc = str(encoding or "").strip().lower()
        if enc == "base64":
            try:
                raw = base64.b64decode(content, validate=True)
            except Exception as exc:
                raise ValueError(f"invalid base64 content: {exc}") from exc
        elif enc in ("utf-8", "utf8", ""):
            try:
                raw = content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("content is not valid UTF-8") from exc
        else:
            raise ValueError("encoding must be utf-8 or base64")
        if len(raw) > self._MAX_INLINE_FILE_BYTES:
            raise ValueError(
                f"content is {len(raw)} bytes; local cap is {self._MAX_INLINE_FILE_BYTES}"
            )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(normalized, raw)
        return buf.getvalue()

    def list_resources(self) -> List[Dict[str, str]]:
        return [
            {
                "uri": "perunchat://rooms/{room_id}",
                "name": "PerunChat Room Stream",
                "description": "Last 500 real-time chat messages for a room.",
                "mimeType": "application/json",
            }
        ]

    def list_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "perunchat_join_room",
                "description": "Join a PerunChat room so the agent can receive and send messages.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                        },
                    },
                    "required": ["room_id", "room_type"],
                },
            },
            {
                "name": "perunchat_leave_room",
                "description": "Leave a PerunChat room.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                        },
                    },
                    "required": ["room_id", "room_type"],
                },
            },
            {
                "name": "perunchat_send_message",
                "description": "Send a text message into an active PerunChat room.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                        },
                        "text": {"type": "string"},
                        "thread_id": {"type": "string"},
                    },
                    "required": ["room_id", "room_type", "text"],
                },
            },
            {
                "name": "perunchat_propose_action",
                "description": (
                    "Emit a structured action proposal for human approval. "
                    "State-changing actions must use this tool; chat messages alone cannot execute commands."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "risk_tier": {
                            "type": "string",
                            "enum": [
                                "TIER_0_READ_ONLY",
                                "TIER_1_SCRATCHPAD",
                                "TIER_2_LOW_IMPACT",
                                "TIER_3_HIGH_IMPACT",
                            ],
                        },
                        "action_domain": {
                            "type": "string",
                            "enum": ["FILESYSTEM", "SHELL", "GIT", "API"],
                        },
                        "description": {"type": "string"},
                        "details": {
                            "type": "object",
                            "description": "Execution details such as diff, command, file_path.",
                        },
                        "justification": {"type": "string"},
                    },
                    "required": [
                        "room_id",
                        "risk_tier",
                        "action_domain",
                        "description",
                        "justification",
                    ],
                },
            },
            {
                "name": "perunchat_claim_execution",
                "description": (
                    "Claim an approved Perun shared-project proposal for external execution. "
                    "Only a trusted rw participant in the managed project room can claim. "
                    "The claim is reserved for this authenticated agent for one hour."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "proposal_id": {"type": "string"},
                    },
                    "required": ["room_id", "proposal_id"],
                },
            },
            {
                "name": "perunchat_request_download_package",
                "description": (
                    "Download the approved targets of a claimed Perun proposal as a ZIP. "
                    "Writes the verified archive to a local path and returns its sha256 and size."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "proposal_id": {"type": "string"},
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional subset of the approved targets.",
                        },
                        "destination": {
                            "type": "string",
                            "description": "Optional output path for the ZIP.",
                        },
                    },
                    "required": ["room_id", "proposal_id"],
                },
            },
            {
                "name": "perunchat_upload_package",
                "description": (
                    "Upload an edited ZIP package for a claimed Perun proposal. "
                    "Perun verifies the sha256, applies only approved target paths, "
                    "and audits the result."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "proposal_id": {"type": "string"},
                        "zip_path": {"type": "string"},
                        "chunk_bytes": {
                            "type": "integer",
                            "minimum": 16384,
                            "maximum": 262144,
                        },
                    },
                    "required": ["room_id", "proposal_id", "zip_path"],
                },
            },
            {
                "name": "perunchat_write_target_file",
                "description": (
                    "Submit one file's new content as an external execution package. "
                    "Useful when the agent cannot edit a downloaded ZIP locally. "
                    "Perun accepts only paths approved on the proposal."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "proposal_id": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "Project-relative target path, e.g. CANARY7.md.",
                        },
                        "content": {"type": "string"},
                        "encoding": {
                            "type": "string",
                            "enum": ["utf-8", "base64"],
                            "default": "utf-8",
                        },
                        "chunk_bytes": {
                            "type": "integer",
                            "minimum": 16384,
                            "maximum": 262144,
                        },
                    },
                    "required": ["room_id", "proposal_id", "path", "content"],
                },
            },
            {
                "name": "perunchat_execution_status",
                "description": (
                    "Read the status of a Perun shared-project proposal "
                    "(pending/approved/claimed/executing/executed/failed/rejected/expired)."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "proposal_id": {"type": "string"},
                    },
                    "required": ["room_id", "proposal_id"],
                },
            },
            {
                "name": "perunchat_transfer_status",
                "description": (
                    "Query which upload chunks Perun has received for a transfer, "
                    "so an interrupted upload can resume."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "transfer_id": {"type": "string"},
                    },
                    "required": ["room_id", "transfer_id"],
                },
            },
            {
                "name": "perunchat_revoke_execution_claim",
                "description": (
                    "Return a claimed Perun proposal to approved. Only the claim holder "
                    "or the room owner may revoke."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "room_id": {"type": "string"},
                        "room_type": {
                            "type": "string",
                            "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                            "default": "GROUP",
                        },
                        "proposal_id": {"type": "string"},
                    },
                    "required": ["room_id", "proposal_id"],
                },
            },
        ]

    async def read_resource(self, uri: str) -> str:
        """Return the buffered messages for a room resource."""
        prefix = "perunchat://rooms/"
        if not uri.startswith(prefix):
            raise ValueError(f"Unsupported PerunChat resource URI: {uri}")
        room_id = uri[len(prefix):]
        await self._ensure_client()
        messages = self._room_messages.get(room_id, [])
        return json.dumps(messages, ensure_ascii=False)

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        known = {t["name"] for t in self.list_tools()}
        if name not in known:
            return _err(f"Unknown tool: {name}")

        client = await self._ensure_client()

        room_id = arguments.get("room_id")
        if not isinstance(room_id, str) or not room_id.strip():
            return _err("room_id is required")
        room_id = room_id.strip()
        self._last_errors.pop(room_id, None)
        room_type_name = arguments.get("room_type", "GROUP")
        try:
            room_type = self._enum_member(
                self._RoomType, room_type_name, "room_type", default="GROUP"
            )
            if name == "perunchat_join_room":
                await client.join_room(room_id, room_type)
                return _ok(f"Joined {room_id}")
            if name == "perunchat_leave_room":
                await client.leave_room(room_id, room_type)
                return _ok(f"Left {room_id}")
            if name == "perunchat_send_message":
                frame = await client.send_text(
                    room_id,
                    room_type,
                    arguments.get("text", ""),
                    thread_id=arguments.get("thread_id", "default"),
                )
                return _ok(f"Message sent to {room_id}: {frame['id']}")
            if name == "perunchat_propose_action":
                tier_name = arguments.get("risk_tier", "TIER_2_LOW_IMPACT")
                risk_tier = self._enum_member(
                    self._RiskTier,
                    tier_name,
                    "risk_tier",
                    default="TIER_2_LOW_IMPACT",
                )
                frame = await client.submit_proposal(
                    room_id=room_id,
                    risk_tier=risk_tier,
                    action_domain=arguments.get("action_domain", "API"),
                    description=arguments.get("description", ""),
                    execution_details=arguments.get("details") or {},
                    justification=arguments.get("justification", ""),
                )
                proposal_id = frame["payload"]["proposal_id"]
                return _ok(
                    f"Action proposal {proposal_id} staged in {room_id}. "
                    "Another rw participant must approve it, then it can be claimed."
                )
            if name == "perunchat_claim_execution":
                proposal_id = self._proposal_id_argument(arguments)
                async with self._proposal_lock_for(proposal_id):
                    since = self._control_seq
                    await self._send_execution(
                        client,
                        room_id,
                        room_type,
                        {"op": "claim_execution", "proposal_id": proposal_id},
                    )
                    payload = await self._wait_for_result(
                        "claim_execution", proposal_id, since=since, room_id=room_id
                    )
                return _result_response(payload)
            if name == "perunchat_request_download_package":
                proposal_id = self._proposal_id_argument(arguments)
                async with self._proposal_lock_for(proposal_id):
                    raw_destination = arguments.get("destination")
                    try:
                        destination = self._resolve_handoff_path(
                            str(raw_destination or ""), f"{proposal_id}.zip"
                        )
                        if destination.is_dir():
                            destination = self._resolve_handoff_path(
                                str(destination / f"{proposal_id}.zip"), ""
                            )
                    except ValueError as exc:
                        return _err(str(exc))
                    request: Dict[str, Any] = {
                        "op": "request_download_package",
                        "proposal_id": proposal_id,
                    }
                    paths = arguments.get("paths")
                    if paths is not None:
                        if not isinstance(paths, list) or not all(
                            isinstance(item, str) for item in paths
                        ):
                            return _err("paths must be a list of strings")
                        request["paths"] = paths
                    since = self._control_seq
                    await self._send_execution(client, room_id, room_type, request)
                    ready = await self._wait_for_package_ready(
                        proposal_id, since=since, room_id=room_id
                    )
                    transfer_id = str(ready.get("transfer_id") or "")
                    try:
                        total_chunks = int(ready.get("total_chunks") or 0)
                    except (TypeError, ValueError):
                        total_chunks = 0
                    if not transfer_id or total_chunks <= 0:
                        return _err("package_ready did not include a valid transfer")
                    try:
                        archive = await self._collect_package(transfer_id, total_chunks)
                    finally:
                        self._drop_package(transfer_id)
                    expected_size = int(ready.get("total_size") or len(archive))
                    if len(archive) != expected_size:
                        return _err(
                            f"download size mismatch: expected {expected_size}, got {len(archive)}"
                        )
                    digest = hashlib.sha256(archive).hexdigest()
                    expected_digest = str(ready.get("sha256") or "").lower()
                    if expected_digest and digest != expected_digest:
                        return _err("download sha256 mismatch")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temp_path = destination.with_name(destination.name + ".part")
                    try:
                        await asyncio.to_thread(temp_path.write_bytes, archive)
                        os.replace(temp_path, destination)
                    finally:
                        temp_path.unlink(missing_ok=True)
                    return _ok(
                        json.dumps(
                            {
                                "ok": True,
                                "proposal_id": proposal_id,
                                "transfer_id": transfer_id,
                                "path": str(destination),
                                "sha256": digest,
                                "total_size": len(archive),
                                "total_chunks": total_chunks,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
            if name == "perunchat_upload_package":
                proposal_id = self._proposal_id_argument(arguments)
                zip_path = self._string_argument(arguments, "zip_path")
                package_path = self._resolve_handoff_path(zip_path, "")
                if not package_path.is_file():
                    return _err(f"ZIP not found: {package_path}")
                archive = await asyncio.to_thread(package_path.read_bytes)
                async with self._proposal_lock_for(proposal_id):
                    result = await self._upload_archive(
                        client,
                        room_id,
                        room_type,
                        proposal_id,
                        archive,
                        self._chunk_bytes_arg(arguments.get("chunk_bytes")),
                    )
                return _result_response(result)
            if name == "perunchat_write_target_file":
                proposal_id = self._proposal_id_argument(arguments)
                target_path = self._string_argument(arguments, "path")
                content = arguments.get("content")
                if content is None:
                    content = ""
                if not isinstance(content, str):
                    return _err("content must be a string")
                encoding = arguments.get("encoding")
                if encoding is None:
                    encoding = "utf-8"
                if not isinstance(encoding, str):
                    return _err("encoding must be a string")
                archive = self._build_single_file_archive(
                    target_path,
                    content,
                    encoding,
                )
                async with self._proposal_lock_for(proposal_id):
                    result = await self._upload_archive(
                        client,
                        room_id,
                        room_type,
                        proposal_id,
                        archive,
                        self._chunk_bytes_arg(arguments.get("chunk_bytes")),
                    )
                return _result_response(result)
            if name == "perunchat_execution_status":
                proposal_id = self._proposal_id_argument(arguments)
                since = self._control_seq
                await self._send_execution(
                    client,
                    room_id,
                    room_type,
                    {"op": "execution_status", "proposal_id": proposal_id},
                )
                payload = await self._wait_for_result(
                    "execution_status", proposal_id, timeout=30.0, since=since, room_id=room_id
                )
                return _result_response(payload)
            if name == "perunchat_transfer_status":
                transfer_id = self._string_argument(arguments, "transfer_id")
                since = self._control_seq
                await self._send_execution(
                    client,
                    room_id,
                    room_type,
                    {"op": "transfer_status", "transfer_id": transfer_id},
                )
                payload = await self._wait_for_result(
                    "transfer_status",
                    "",
                    timeout=30.0,
                    since=since,
                    transfer_id=transfer_id,
                    room_id=room_id,
                )
                return _result_response(payload)
            if name == "perunchat_revoke_execution_claim":
                proposal_id = self._proposal_id_argument(arguments)
                async with self._proposal_lock_for(proposal_id):
                    since = self._control_seq
                    await self._send_execution(
                        client,
                        room_id,
                        room_type,
                        {"op": "revoke_execution_claim", "proposal_id": proposal_id},
                    )
                    payload = await self._wait_for_result(
                        "revoke_execution_claim", proposal_id, timeout=30.0, since=since, room_id=room_id
                    )
                return _result_response(payload)
        except Exception as exc:
            return _err(f"PerunChat bridge error: {exc}")


def _ok(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _result_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return _ok(text) if payload.get("ok") else _err(text)


def serve_mcp(
    endpoint: str, api_key: str, agent_id: str, transport: str = "stdio"
) -> None:
    """Entry point for the MCP stdio server."""
    server = PerunChatMCPServer(endpoint, api_key, agent_id)

    if _HAS_MCP:
        if not hasattr(Server, "list_resources"):
            raise RuntimeError(
                "perunchat.mcp_bridge requires the mcp 1.x server API. "
                "Install with `pip install \"mcp>=1.9,<2\"` or reinstall "
                "perunchat[mcp]."
            )
        mcp = Server("perunchat")

        @mcp.list_resources()
        async def list_resources() -> List[Any]:
            # The SDK's resource type is richer, but our dicts serialize fine.
            return server.list_resources()  # type: ignore[return-value]

        @mcp.read_resource()
        async def read_resource(uri: str) -> str:
            return await server.read_resource(uri)

        @mcp.list_tools()
        async def list_tools() -> List[Tool]:
            return [
                Tool(
                    name=t["name"],
                    description=t["description"],
                    inputSchema=t["inputSchema"],
                )
                for t in server.list_tools()
            ]

        @mcp.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
            result = await server.call_tool(name, arguments)
            return [TextContent(type="text", text=result["content"][0]["text"])]

        if transport == "stdio":
            import anyio
            from mcp.server.stdio import stdio_server

            async def _run_stdio() -> None:
                async with stdio_server() as (read_stream, write_stream):
                    await mcp.run(
                        read_stream,
                        write_stream,
                        mcp.create_initialization_options(),
                    )

            anyio.run(_run_stdio)
        elif transport == "sse":
            raise ValueError(
                "SSE transport is not supported by this bridge build; use stdio"
            )
        else:
            raise ValueError(f"Unsupported MCP transport: {transport}")
    else:
        # Fallback for environments without the official `mcp` package installed.
        print(
            f"[MCP] PerunChat bridge ready ({transport}) for agent {agent_id} at {endpoint}"
        )
        print("[MCP] Tools:", [t["name"] for t in server.list_tools()])
        print("[MCP] Resources:", [r["uri"] for r in server.list_resources()])
        print(
            "[MCP] Install the `mcp` package (modelcontextprotocol) to run a real MCP server."
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="PerunChat MCP bridge")
    parser.add_argument("--endpoint", default="wss://peruninthemachine.com/v1/chat/ws")
    parser.add_argument("--api-key", required=False)
    parser.add_argument("--agent-id", required=False)
    parser.add_argument("--transport", default="stdio", choices=["stdio"])
    args = parser.parse_args()
    api_key = args.api_key or os.environ.get("PERUNCHAT_API_KEY")
    agent_id = args.agent_id or os.environ.get("PERUNCHAT_AGENT_ID")
    if not api_key or not agent_id:
        print(
            "Error: --api-key/--agent-id or PERUNCHAT_API_KEY/PERUNCHAT_AGENT_ID required.",
            file=sys.stderr,
        )
        sys.exit(1)
    serve_mcp(args.endpoint, api_key, agent_id, args.transport)


if __name__ == "__main__":
    main()


__all__ = ["PerunChatMCPServer", "serve_mcp"]
