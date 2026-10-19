"""Tests for the MCP bridge skeleton."""

from __future__ import annotations

from perunchat.mcp_bridge import PerunChatMCPServer, serve_mcp


def test_list_resources() -> None:
    server = PerunChatMCPServer("ws://localhost/ws", "key", "agent-1")
    resources = server.list_resources()
    assert any("perunchat://rooms/{room_id}" == r["uri"] for r in resources)


def test_list_tools() -> None:
    server = PerunChatMCPServer("ws://localhost/ws", "key", "agent-1")
    tools = server.list_tools()
    names = {t["name"] for t in tools}
    assert "perunchat_send_message" in names
    assert "perunchat_propose_action" in names


def test_call_tool_send_message() -> None:
    server = PerunChatMCPServer("ws://localhost/ws", "key", "agent-1")
    # Without a live gateway the bridge returns an error. The important check
    # is that the tool schema is wired correctly and the response shape matches
    # the MCP result contract.
    result = server.call_tool(
        "perunchat_send_message",
        {"room_id": "grp:test:room", "room_type": "GROUP", "text": "hi"},
    )
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"


def test_call_tool_unknown() -> None:
    server = PerunChatMCPServer("ws://localhost/ws", "key", "agent-1")
    result = server.call_tool("unknown_tool", {})
    assert result["isError"] is True
