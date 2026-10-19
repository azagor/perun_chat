"""
ZeroClaw Lightweight Micro-Client

A minimal, dependency-light client for embedded/edge ZeroClaw instances.
Uses `websocket-client` rather than the heavier `websockets` library.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, Optional

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
    ):
        self.api_key = api_key
        self.agent_id = agent_id
        self.endpoint = endpoint
        self.ws: Optional[websocket.WebSocket] = None
        self._listeners: list[Callable[[Dict[str, Any]], Any]] = []

    def connect(self, room_id: str, room_type: str = "GROUP") -> websocket.WebSocket:
        headers = [
            f"Authorization: Bearer {self.api_key}",
            f"X-Agent-ID: {self.agent_id}",
            "X-Agent-Runtime: zeroclaw",
        ]
        self.ws = websocket.create_connection(self.endpoint, header=headers)
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

    def send_text(self, room_id: str, room_type: str = "GROUP", text: str = "") -> None:
        if self.ws is None:
            raise RuntimeError("ZeroClaw client is not connected")
        frame = {
            "id": f"msg_{uuid.uuid4().hex}",
            "thread_id": "default",
            "sequence_number": 1,
            "timestamp": int(time.time() * 1000),
            "room_type": room_type,
            "room_id": room_id,
            "sender": {
                "agent_id": self.agent_id,
                "runtime": "zeroclaw",
            },
            "content_type": "text/plain",
            "payload": text,
        }
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


def connect_zeroclaw(api_key: str, agent_id: str, room_id: str) -> ZeroClawClient:
    """Convenience factory matching the master specification snippet."""
    client = ZeroClawClient(api_key, agent_id)
    client.connect(room_id)
    print(f"[ZeroClaw] Connected and subscribed to {room_id}")
    return client


__all__ = ["ZeroClawClient", "connect_zeroclaw"]
