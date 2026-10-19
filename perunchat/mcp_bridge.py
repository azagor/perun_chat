"""
Model Context Protocol (MCP) Bridge

Provides a functional MCP server wrapper for PerunChat:
  - Resource: perunchat://rooms/{room_id}
  - Tool: perunchat_send_message(room_id, room_type, text, thread_id)
  - Tool: perunchat_join_room(room_id, room_type)
  - Tool: perunchat_leave_room(room_id, room_type)
  - Tool: perunchat_propose_action(room_id, risk_tier, action_domain, description, details, justification)

This bridge uses the official `mcp` package when available; if not, it falls back
to the original stub implementation so import-time dependencies stay light.
"""

from __future__ import annotations

from typing import Any, Dict, List

try:
    from mcp.server import Server
    from mcp.types import (
        TextContent,
        Tool,
    )

    _HAS_MCP = True
except Exception:  # pragma: no cover
    _HAS_MCP = False


class PerunChatMCPServer:
    """
    MCP server for PerunChat.

    Production implementation should subclass the official MCP SDK
    (Server, mcp.server.Server) and register resources/tools/prompts.
    """

    def __init__(self, endpoint: str, api_key: str, agent_id: str) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.agent_id = agent_id
        self._client: Any = None
        self._RoomType: Any = None

    def _ensure_client(self) -> Any:
        """Lazy import of the async Python SDK so the bridge stays optional."""
        if self._client is not None:
            return self._client
        try:
            from perunchat.client import PerunChatClient, RoomType
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
        )
        self._RoomType = RoomType
        return self._client

    def list_resources(self) -> List[Dict[str, str]]:
        return [
            {
                "uri": "perunchat://rooms/{room_id}",
                "name": "PerunChat Room Stream",
                "description": "Subscribe to real-time chat messages for a room.",
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

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        import asyncio

        client = self._ensure_client()

        async def _run() -> str:
            if client.ws is None or getattr(client.ws, "close_code", None) is not None:
                await client.connect()

            room_id = arguments.get("room_id")
            room_type_name = arguments.get("room_type", "GROUP")
            room_type = getattr(self._RoomType, room_type_name, self._RoomType.GROUP)
            if name == "perunchat_join_room":
                await client.join_room(room_id, room_type)
                return f"Joined {room_id}"
            if name == "perunchat_leave_room":
                await client.leave_room(room_id, room_type)
                return f"Left {room_id}"
            if name == "perunchat_send_message":
                await client.send_text(
                    room_id,
                    room_type,
                    arguments.get("text", ""),
                    thread_id=arguments.get("thread_id", "default"),
                )
                return f"Message sent to {room_id}"
            if name == "perunchat_propose_action":
                from perunchat.client import RiskTier

                tier_name = arguments.get("risk_tier", "TIER_2_LOW_IMPACT")
                risk_tier = getattr(RiskTier, tier_name, RiskTier.TIER_2_LOW_IMPACT)
                await client.submit_proposal(
                    room_id=room_id,
                    risk_tier=risk_tier,
                    action_domain=arguments.get("action_domain", "API"),
                    description=arguments.get("description", ""),
                    execution_details=arguments.get("details") or {},
                    justification=arguments.get("justification", ""),
                )
                return f"Action proposal emitted to {room_id} for human approval"
            return f"Unknown tool: {name}"

        try:
            result = asyncio.get_event_loop().run_until_complete(_run())
            return {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            }
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"PerunChat bridge error: {exc}"}],
                "isError": True,
            }


def serve_mcp(
    endpoint: str, api_key: str, agent_id: str, transport: str = "stdio"
) -> None:
    """Entry point for an MCP stdio/SSE server."""
    server = PerunChatMCPServer(endpoint, api_key, agent_id)

    if _HAS_MCP:
        mcp = Server("perunchat")

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
            result = server.call_tool(name, arguments)
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
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--transport", default="stdio", choices=["stdio", "sse"])
    args = parser.parse_args()
    serve_mcp(args.endpoint, args.api_key, args.agent_id, args.transport)


if __name__ == "__main__":
    main()


__all__ = ["PerunChatMCPServer", "serve_mcp"]
