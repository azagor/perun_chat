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

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class OpenClawNodeContext:
    """Context object passed by OpenClaw when invoking a plugin hook."""

    node_id: str
    runtime: str
    api_key: str
    endpoint: str
    rooms: List[str]


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

    def send_message(
        self,
        room_id: str,
        room_type: str,
        message: str,
        thread_id: str = "default",
    ) -> Dict[str, Any]:
        frame = {
            "type": "CHAT",
            "room_id": room_id,
            "room_type": room_type,
            "thread_id": thread_id,
            "content_type": "text/plain",
            "payload": message,
            "sender": {
                "agent_id": self.context.node_id,
                "runtime": self.context.runtime,
                "version": self.version,
            },
        }
        return self._emit(frame)

    def join_room(self, room_id: str, room_type: str = "GROUP") -> Dict[str, Any]:
        frame = {
            "type": "ROOM_JOIN",
            "room_id": room_id,
            "room_type": room_type,
        }
        return self._emit(frame)

    def leave_room(self, room_id: str, room_type: str = "GROUP") -> Dict[str, Any]:
        frame = {
            "type": "ROOM_LEAVE",
            "room_id": room_id,
            "room_type": room_type,
        }
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
        frame = {
            "type": "CHAT",
            "room_id": room_id,
            "room_type": "GROUP",
            "thread_id": thread_id,
            "content_type": "application/perun-proposal",
            "payload": {
                "proposal_type": "PERUN_ACTION_PROPOSAL",
                "risk_tier": risk_tier,
                "action_domain": action_domain,
                "description": description,
                "execution_details": execution_details,
                "provenance": {
                    "instigator": self.context.node_id,
                    "justification": justification,
                },
            },
            "sender": {
                "agent_id": self.context.node_id,
                "runtime": self.context.runtime,
                "version": self.version,
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


def make_openclaw_plugin(config: Dict[str, Any]) -> PerunChatOpenClawPlugin:
    """Factory used by OpenClaw plugin discovery."""
    context = OpenClawNodeContext(
        node_id=config["node_id"],
        runtime=config.get("runtime", "openclaw"),
        api_key=config["api_key"],
        endpoint=config.get("endpoint", "wss://peruninthemachine.com/v1/chat/ws"),
        rooms=config.get("rooms", []),
    )
    return PerunChatOpenClawPlugin(context)


__all__ = [
    "OpenClawNodeContext",
    "PerunChatOpenClawPlugin",
    "make_openclaw_plugin",
]
