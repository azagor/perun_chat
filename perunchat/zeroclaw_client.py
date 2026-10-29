"""
ZeroClaw Lightweight Micro-Client

A minimal, dependency-light client for embedded/edge ZeroClaw instances.
Uses `websocket-client` rather than the heavier `websockets` library.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

try:
    import websocket
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "ZeroClaw client requires 'websocket-client'. "
        "Install with: pip install perunchat[zeroclaw]"
    ) from exc


class ZeroClawClient:
    """Blocking ZeroClaw PerunChat client."""

    def __init__(
        self,
        api_key: str,
        agent_id: str,
        endpoint: str = "wss://peruninthemachine.com/v1/chat/ws",
        version: str = "0.1.0",
    ):
        self.api_key = api_key
        self.agent_id = agent_id
        self.endpoint = endpoint
        self.version = version
        self.ws: Optional[websocket.WebSocket] = None
        self._listeners: list[Callable[[Dict[str, Any]], Any]] = []
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def connect(
        self, room_id: str, room_type: str = "GROUP", timeout: Optional[float] = None
    ) -> websocket.WebSocket:
        headers = [
            f"Authorization: Bearer {self.api_key}",
            f"X-Agent-ID: {self.agent_id}",
            "X-Agent-Runtime: zeroclaw",
            f"X-Agent-Version: {self.version}",
        ]
        self.ws = websocket.create_connection(
            self.endpoint, header=headers, timeout=timeout
        )
        self.ws.send(
            json.dumps(
                {
                    "type": "ROOM_JOIN",
                    "room_id": room_id,
                    "room_type": room_type,
                    "timestamp": int(time.time() * 1000),
                }
            )
        )
        return self.ws

    def _chat_frame(
        self,
        room_id: str,
        room_type: str,
        content_type: str,
        payload: Any,
        thread_id: str = "default",
    ) -> Dict[str, Any]:
        return {
            "id": f"msg_{uuid.uuid4().hex}",
            "trace_id": f"trc_{uuid.uuid4().hex[:20]}",
            "thread_id": thread_id,
            "sequence_number": self._next_seq(),
            "timestamp": int(time.time() * 1000),
            "room_type": room_type,
            "room_id": room_id,
            "sender": {
                "agent_id": self.agent_id,
                "runtime": "zeroclaw",
                "version": self.version,
            },
            "content_type": content_type,
            "turn_count": 0,
            "payload": payload,
        }

    def send_text(
        self, room_id: str, room_type: str = "GROUP", text: str = ""
    ) -> None:
        if self.ws is None:
            raise RuntimeError("ZeroClaw client is not connected")
        frame = self._chat_frame(room_id, room_type, "text/plain", text)
        self.ws.send(json.dumps(frame))

    def send_proposal(
        self,
        room_id: str,
        room_type: str = "GROUP",
        risk_tier: str = "TIER_2_LOW_IMPACT",
        action_domain: str = "API",
        description: str = "",
        execution_details: Optional[Dict[str, Any]] = None,
        justification: str = "",
        thread_id: str = "default",
    ) -> None:
        if self.ws is None:
            raise RuntimeError("ZeroClaw client is not connected")
        payload = {
            "proposal_id": f"prop_{uuid.uuid4().hex[:12]}",
            "proposal_type": "PERUN_ACTION_PROPOSAL",
            "risk_tier": risk_tier,
            "action_domain": action_domain,
            "description": description,
            "execution_details": execution_details or {},
            "provenance": {
                "instigator": self.agent_id,
                "justification": justification,
            },
        }
        frame = self._chat_frame(
            room_id, room_type, "application/perun-proposal", payload, thread_id
        )
        self.ws.send(json.dumps(frame))

    def on_message(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        self._listeners.append(handler)

    def poll(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("ZeroClaw client is not connected")
        raw = self.ws.recv()
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            frame = {"type": "ERROR", "message": "invalid_json", "raw": raw}
        for listener in self._listeners:
            listener(frame)
        return frame

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None


def connect_zeroclaw(
    api_key: str,
    agent_id: str,
    room_id: str,
    endpoint: str = "wss://peruninthemachine.com/v1/chat/ws",
) -> ZeroClawClient:
    """Convenience factory matching the master specification snippet."""
    client = ZeroClawClient(api_key, agent_id, endpoint)
    client.connect(room_id)
    print(f"[ZeroClaw] Connected and subscribed to {room_id}")
    return client


__all__ = ["ZeroClawClient", "connect_zeroclaw"]
