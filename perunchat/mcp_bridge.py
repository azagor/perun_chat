"""
Model Context Protocol (MCP) Bridge

Provides a functional MCP server wrapper for PerunChat:
  - Resource: perunchat://rooms/{room_id}
  - Tool: perunchat_send_message(room_id, room_type, text, thread_id)
  - Tool: perunchat_join_room(room_id, room_type)
  - Tool: perunchat_leave_room(room_id, room_type)
  - Tool: perunchat_propose_action(room_id, risk_tier, action_domain, description, details, justification)

This bridge uses the official `mcp` package when available; if not, it falls back
to a minimal JSON-RPC-ish stub so import-time dependencies stay light.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Optional

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

    def __init__(self, endpoint: str, api_key: str, agent_id: str) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.agent_id = agent_id
        self._client: Any = None
        self._RoomType: Any = None
        self._RiskTier: Any = None
        self._room_messages: Dict[str, List[Dict[str, Any]]] = {}

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

        def _store_message(frame: Dict[str, Any]) -> None:
            room_id = frame.get("room_id")
            if room_id:
                self._room_messages.setdefault(room_id, []).append(frame)
                # Bound memory per room.
                self._room_messages[room_id] = self._room_messages[room_id][-500:]

        self._client.on_message(_store_message)
        await self._client.connect()
        return self._client

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
        room_type_name = arguments.get("room_type", "GROUP")
        room_type = getattr(self._RoomType, room_type_name, self._RoomType.GROUP)
        try:
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
                risk_tier = getattr(
                    self._RiskTier, tier_name, self._RiskTier.TIER_2_LOW_IMPACT
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
                    "Approve via the dashboard, then resubmit with the token."
                )
        except Exception as exc:
            return _err(f"PerunChat bridge error: {exc}")


def _ok(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _err(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def serve_mcp(
    endpoint: str, api_key: str, agent_id: str, transport: str = "stdio"
) -> None:
    """Entry point for an MCP stdio/SSE server."""
    server = PerunChatMCPServer(endpoint, api_key, agent_id)

    if _HAS_MCP:
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
            mcp.run(transport="stdio")
        elif transport == "sse":
            mcp.run(transport="sse")
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
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
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
