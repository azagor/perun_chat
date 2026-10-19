"""Tests for the PerunChat Python SDK client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from perunchat.client import PerunChatClient, RiskTier, RoomType


@pytest.fixture
def client() -> PerunChatClient:
    return PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="test-agent",
        runtime="perun",
    )


@pytest.mark.asyncio
async def test_connect_uses_correct_headers(client: PerunChatClient) -> None:
    mock_ws = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws.__aexit__ = AsyncMock(return_value=False)

    async def fake_connect(*args, **kwargs):
        return mock_ws

    with patch("websockets.connect", side_effect=fake_connect) as connect_mock:
        await client.connect()

    connect_mock.assert_called_once()
    _, kwargs = connect_mock.call_args
    assert kwargs["extra_headers"]["Authorization"] == "Bearer prn_live_secret"
    assert kwargs["extra_headers"]["X-Agent-ID"] == "test-agent"
    assert kwargs["extra_headers"]["X-Agent-Runtime"] == "perun"
    assert kwargs["extra_headers"]["X-Agent-Version"] == "0.1.0"


@pytest.mark.asyncio
async def test_send_text_frame(client: PerunChatClient) -> None:
    mock_ws = AsyncMock()
    client.ws = mock_ws
    client._seq = 0

    frame = await client.send_text(
        room_id="grp:belle-core:demo",
        room_type=RoomType.GROUP,
        text="Hello, agents!",
    )

    assert frame["room_id"] == "grp:belle-core:demo"
    assert frame["room_type"] == "GROUP"
    assert frame["content_type"] == "text/plain"
    assert frame["payload"] == "Hello, agents!"
    assert frame["sequence_number"] == 1
    assert frame["sender"]["agent_id"] == "test-agent"
    mock_ws.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_proposal_frame(client: PerunChatClient) -> None:
    mock_ws = AsyncMock()
    client.ws = mock_ws
    client._seq = 0

    frame = await client.submit_proposal(
        room_id="grp:belle-core:demo",
        risk_tier=RiskTier.TIER_2_LOW_IMPACT,
        action_domain="FILESYSTEM",
        description="Update README",
        execution_details={"file_path": "README.md", "diff": "+line"},
        justification="Docs need refresh",
    )

    assert frame["content_type"] == "application/perun-proposal"
    payload = frame["payload"]
    assert payload["proposal_type"] == "PERUN_ACTION_PROPOSAL"
    assert payload["risk_tier"] == "TIER_2_LOW_IMPACT"
    assert payload["action_domain"] == "FILESYSTEM"
    assert payload["execution_details"]["file_path"] == "README.md"
    assert payload["provenance"]["instigator"] == "test-agent"
    mock_ws.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_message_listener(client: PerunChatClient) -> None:
    received: list = []

    def handler(frame: dict) -> None:
        received.append(frame)

    client.on_message(handler)

    mock_ws = AsyncMock()
    mock_ws.__aiter__.return_value = iter([json.dumps({"payload": "hi"})])
    client.ws = mock_ws

    await client._listen_loop()

    assert len(received) == 1
    assert received[0]["payload"] == "hi"


@pytest.mark.asyncio
async def test_disconnect(client: PerunChatClient) -> None:
    mock_ws = AsyncMock()
    client.ws = mock_ws
    async def fake_task():
        return None

    task = asyncio.create_task(fake_task())
    client._listen_task = task

    await client.disconnect()

    assert task.cancelled() or task.done()
    mock_ws.close.assert_awaited_once()
