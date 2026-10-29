"""Tests for the MCP bridge skeleton."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from perunchat.mcp_bridge import PerunChatMCPServer, serve_mcp


@pytest.fixture
def server() -> PerunChatMCPServer:
    return PerunChatMCPServer("ws://localhost/ws", "key", "agent-1")


@pytest.mark.asyncio
async def test_list_resources(server: PerunChatMCPServer) -> None:
    resources = server.list_resources()
    assert any("perunchat://rooms/{room_id}" == r["uri"] for r in resources)


@pytest.mark.asyncio
async def test_list_tools(server: PerunChatMCPServer) -> None:
    tools = server.list_tools()
    names = {t["name"] for t in tools}
    assert "perunchat_send_message" in names
    assert "perunchat_propose_action" in names


@pytest.mark.asyncio
async def test_call_tool_send_message(server: PerunChatMCPServer) -> None:
    mock_client = AsyncMock()
    mock_client.ws = AsyncMock()
    mock_client.ws.close_code = None
    mock_client.send_text = AsyncMock(return_value={"id": "msg_1"})
    mock_client.on_message = Mock()
    with patch("perunchat.client.PerunChatClient", return_value=mock_client):
        result = await server.call_tool(
            "perunchat_send_message",
            {"room_id": "grp:test:room", "room_type": "GROUP", "text": "hi"},
        )
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert result["isError"] is False
    mock_client.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_tool_unknown(server: PerunChatMCPServer) -> None:
    result = await server.call_tool("unknown_tool", {})
    assert result["isError"] is True


@pytest.mark.asyncio
async def test_read_resource_starts_client(server: PerunChatMCPServer) -> None:
    mock_client = AsyncMock()
    mock_client.ws = AsyncMock()
    mock_client.ws.close_code = None
    mock_client.on_message = Mock()
    with patch("perunchat.client.PerunChatClient", return_value=mock_client):
        text = await server.read_resource("perunchat://rooms/grp:test:room")
    assert text == "[]"


def test_serve_mcp_stub_does_not_crash() -> None:
    # Without the official `mcp` package installed this should print and return.
    with patch("perunchat.mcp_bridge._HAS_MCP", False):
        with patch("builtins.print") as print_mock:
            serve_mcp("ws://localhost/ws", "key", "agent-1", "stdio")
    assert any("perunchat_send_message" in str(c) for c in print_mock.call_args_list)
