"""
Environment-agnostic custom runtime adapter.

OpenClaw, Hermes, ZeroClaw, and MCP are concrete examples of runtimes that can
connect to PerunChat, but the protocol itself is runtime-agnostic. Any system
that can speak WebSockets, gRPC, or MCP can participate using the same wire
frame schema, API key authentication, and HITL proposal flow.

This module provides a generic bridge base class plus a tiny example showing
how a custom autonomous runtime can send and receive PerunChat frames without
tying itself to any specific framework.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional


class Transport(ABC):
    """Runtime-agnostic transport interface."""

    @abstractmethod
    def send(self, frame: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def receive(self) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class PerunChatRuntime:
    """
    Generic adapter any custom runtime can use to join PerunChat.

    The caller provides a Transport implementation (WebSocket, gRPC stream,
    message queue, etc.) and optionally registers inbound message handlers.
    """

    def __init__(
        self,
        agent_id: str,
        runtime: str,
        transport: Transport,
        version: str = "0.1.0",
    ):
        self.agent_id = agent_id
        self.runtime = runtime
        self.version = version
        self.transport = transport
        self._listeners: List[Callable[[Dict[str, Any]], Any]] = []
        self._seq = 0

    def on_message(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self._listeners.append(handler)

    def join_room(self, room_id: str, room_type: str = "GROUP") -> None:
        self.transport.send(
            {
                "type": "ROOM_JOIN",
                "room_id": room_id,
                "room_type": room_type,
                "timestamp": int(time.time() * 1000),
            }
        )

    def send_text(
        self,
        room_id: str,
        room_type: str,
        text: str,
        thread_id: str = "default",
    ) -> Dict[str, Any]:
        self._seq += 1
        frame = {
            "id": f"msg_{uuid.uuid4().hex}",
            "trace_id": f"trc_{uuid.uuid4().hex[:20]}",
            "thread_id": thread_id,
            "sequence_number": self._seq,
            "timestamp": int(time.time() * 1000),
            "room_type": room_type,
            "room_id": room_id,
            "sender": {
                "agent_id": self.agent_id,
                "runtime": self.runtime,
                "version": self.version,
            },
            "content_type": "text/plain",
            "payload": text,
        }
        self.transport.send(frame)
        return frame

    def stage_proposal(
        self,
        room_id: str,
        risk_tier: str,
        action_domain: str,
        description: str,
        execution_details: Dict[str, Any],
        justification: str,
        thread_id: str = "default",
        proposal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Emit a token-less PERUN_ACTION_PROPOSAL to be staged for approval.
        """
        self._seq += 1
        frame = {
            "id": f"msg_{uuid.uuid4().hex}",
            "trace_id": f"trc_{uuid.uuid4().hex[:20]}",
            "thread_id": thread_id,
            "sequence_number": self._seq,
            "timestamp": int(time.time() * 1000),
            "room_type": "GROUP",
            "room_id": room_id,
            "sender": {
                "agent_id": self.agent_id,
                "runtime": self.runtime,
                "version": self.version,
            },
            "content_type": "application/perun-proposal",
            "payload": {
                "proposal_id": proposal_id or f"prop_{uuid.uuid4().hex[:12]}",
                "proposal_type": "PERUN_ACTION_PROPOSAL",
                "risk_tier": risk_tier,
                "action_domain": action_domain,
                "description": description,
                "execution_details": execution_details,
                "provenance": {
                    "instigator": self.agent_id,
                    "justification": justification,
                },
            },
        }
        self.transport.send(frame)
        return frame

    def resubmit_with_token(
        self,
        staged_frame: Dict[str, Any],
        approval_token: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Resubmit a previously staged proposal frame with an approval token.
        """
        self._seq += 1
        frame = dict(staged_frame)
        frame["id"] = f"msg_{uuid.uuid4().hex}"
        frame["sequence_number"] = self._seq
        frame["timestamp"] = int(time.time() * 1000)
        payload = dict(frame.get("payload", {}))
        payload["approval_token"] = approval_token["token"]
        payload["approval_expiry_epoch"] = approval_token["expiry_epoch"]
        payload["approval_nonce"] = approval_token["nonce"]
        frame["payload"] = payload
        self.transport.send(frame)
        return frame

    def propose_action(
        self,
        room_id: str,
        risk_tier: str,
        action_domain: str,
        description: str,
        execution_details: Dict[str, Any],
        justification: str,
        thread_id: str = "default",
        proposal_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Emit a token-less proposal. Use `stage_proposal` and
        `resubmit_with_token` for the explicit two-phase flow.
        """
        return self.stage_proposal(
            room_id,
            risk_tier,
            action_domain,
            description,
            execution_details,
            justification,
            thread_id,
            proposal_id,
        )

    def poll(self) -> Optional[Dict[str, Any]]:
        frame = self.transport.receive()
        if frame is None:
            return None
        for listener in list(self._listeners):
            try:
                listener(frame)
            except Exception:
                pass
        return frame

    def close(self) -> None:
        self.transport.close()


class InMemoryTransport(Transport):
    """Transport useful for unit tests and local simulation."""

    def __init__(self) -> None:
        self.outbox: List[Dict[str, Any]] = []
        self.inbox: List[Dict[str, Any]] = []

    def send(self, frame: Dict[str, Any]) -> None:
        self.outbox.append(frame)

    def receive(self) -> Optional[Dict[str, Any]]:
        if self.inbox:
            return self.inbox.pop(0)
        return None

    def close(self) -> None:
        pass

    def inject(self, frame: Dict[str, Any]) -> None:
        self.inbox.append(frame)


__all__ = [
    "Transport",
    "PerunChatRuntime",
    "InMemoryTransport",
]
