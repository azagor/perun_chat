"""
OpenClaw Adapter

OpenClaw is a distributed task and orchestration runtime. This adapter
registers PerunChat as a plugin hook so that OpenClaw nodes can:

  - post messages into PerunChat rooms,
  - join/leave rooms,
  - submit PERUN_ACTION_PROPOSAL objects for human approval.

The module exposes a small plugin-compatible class and CLI helper. A full
OpenClaw plugin manifest should declare `perunchat_openclaw` as a hook
consumer for the `agent.chat` and `agent.propose` extension points.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore[assignment]


@dataclass
class OpenClawNodeContext:
    """Context object passed by OpenClaw when invoking a plugin hook."""

    node_id: str
    runtime: str
    api_key: str
    endpoint: str
    rooms: List[str]
    version: str = "0.1.0"


class PerunChatOpenClawPlugin:
    """
    OpenClaw plugin entry point for PerunChat.

    This class is intentionally dependency-light at import time so it can be
    loaded by OpenClaw without pulling in `websockets`. Per-message I/O is
    delegated to the caller-supplied transport callback.
    """

    def __init__(
        self,
        context: OpenClawNodeContext,
        transport: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.context = context
        self.transport = transport
        self._message_handlers: List[Callable[[Dict[str, Any]], Any]] = []
        self._seq = 0

    @property
    def name(self) -> str:
        return "perunchat_openclaw"

    @property
    def version(self) -> str:
        return "0.1.0"

    def hooks(self) -> Dict[str, Callable[..., Any]]:
        return {
            "agent.chat.send": self.send_message,
            "agent.chat.join": self.join_room,
            "agent.chat.leave": self.leave_room,
            "agent.propose": self.propose_action,
            "agent.chat.on_message": self.register_message_handler,
        }

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _base_frame(
        self,
        msg_type: str,
        room_id: str,
        room_type: str,
        thread_id: str = "default",
    ) -> Dict[str, Any]:
        return {
            "type": msg_type,
            "id": f"msg_{uuid.uuid4().hex}",
            "trace_id": f"trc_{uuid.uuid4().hex[:20]}",
            "thread_id": thread_id,
            "sequence_number": self._next_seq(),
            "timestamp": int(time.time() * 1000),
            "room_id": room_id,
            "room_type": room_type,
            "sender": {
                "agent_id": self.context.node_id,
                "runtime": self.context.runtime,
                "version": self.context.version,
            },
            "turn_count": 0,
        }

    def send_message(
        self,
        room_id: str,
        room_type: str,
        message: str,
        thread_id: str = "default",
    ) -> Dict[str, Any]:
        frame = self._base_frame("CHAT", room_id, room_type, thread_id)
        frame["content_type"] = "text/plain"
        frame["payload"] = message
        return self._emit(frame)

    def join_room(self, room_id: str, room_type: str = "GROUP") -> Dict[str, Any]:
        frame = self._base_frame("ROOM_JOIN", room_id, room_type)
        # ROOM_JOIN does not need a sequence_number; remove it to avoid confusion.
        frame.pop("sequence_number", None)
        return self._emit(frame)

    def leave_room(self, room_id: str, room_type: str = "GROUP") -> Dict[str, Any]:
        frame = self._base_frame("ROOM_LEAVE", room_id, room_type)
        frame.pop("sequence_number", None)
        return self._emit(frame)

    def propose_action(
        self,
        room_id: str,
        risk_tier: str,
        action_domain: str,
        description: str,
        execution_details: Dict[str, Any],
        justification: str,
        thread_id: str = "default",
    ) -> Dict[str, Any]:
        frame = self._base_frame("CHAT", room_id, "GROUP", thread_id)
        frame["content_type"] = "application/perun-proposal"
        frame["payload"] = {
            "proposal_id": f"prop_{uuid.uuid4().hex[:12]}",
            "proposal_type": "PERUN_ACTION_PROPOSAL",
            "risk_tier": risk_tier,
            "action_domain": action_domain,
            "description": description,
            "execution_details": execution_details,
            "provenance": {
                "instigator": self.context.node_id,
                "justification": justification,
            },
        }
        return self._emit(frame)

    def register_message_handler(
        self, handler: Callable[[Dict[str, Any]], Any]
    ) -> None:
        self._message_handlers.append(handler)

    def dispatch_inbound(self, frame: Dict[str, Any]) -> None:
        """Called by OpenClaw transport when a chat frame arrives."""
        for handler in list(self._message_handlers):
            handler(frame)

    def _emit(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        if self.transport is not None:
            self.transport(frame)
        return frame


class WebSocketTransport:
    """Blocking websocket-client transport for the OpenClaw plugin."""

    def __init__(self, context: OpenClawNodeContext) -> None:
        self.context = context
        self.ws: Optional[Any] = None

    def connect(self, timeout: Optional[float] = None) -> None:
        if websocket is None:  # pragma: no cover
            raise ImportError(
                "OpenClaw WebSocket transport requires 'websocket-client'. "
                "Install with: pip install perunchat[zeroclaw]"
            )
        headers = [
            f"Authorization: Bearer {self.context.api_key}",
            f"X-Agent-ID: {self.context.node_id}",
            f"X-Agent-Runtime: {self.context.runtime}",
            f"X-Agent-Version: {self.context.version}",
        ]
        self.ws = websocket.create_connection(
            self.context.endpoint, header=headers, timeout=timeout
        )

    def __call__(self, frame: Dict[str, Any]) -> None:
        if self.ws is None:
            self.connect()
        self.ws.send(json.dumps(frame, ensure_ascii=False))

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None


def make_openclaw_plugin(config: Dict[str, Any]) -> PerunChatOpenClawPlugin:
    """Factory used by OpenClaw plugin discovery."""
    context = OpenClawNodeContext(
        node_id=config["node_id"],
        runtime=config.get("runtime", "openclaw"),
        api_key=config["api_key"],
        endpoint=config.get("endpoint", "wss://peruninthemachine.com/v1/chat/ws"),
        rooms=config.get("rooms", []),
        version=config.get("version", "0.1.0"),
    )
    transport: Optional[Callable[[Dict[str, Any]], Any]] = None
    if config.get("use_websocket_transport"):
        transport = WebSocketTransport(context)
    return PerunChatOpenClawPlugin(context, transport=transport)


__all__ = [
    "OpenClawNodeContext",
    "PerunChatOpenClawPlugin",
    "WebSocketTransport",
    "make_openclaw_plugin",
]
