"""Tests for the PerunChat Python SDK client."""

from __future__ import annotations

import asyncio
import json
import time
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
    headers = kwargs.get("additional_headers") or kwargs.get("extra_headers")
    assert headers["Authorization"] == "Bearer prn_live_secret"
    assert headers["X-Agent-ID"] == "test-agent"
    assert headers["X-Agent-Runtime"] == "perun"
    assert headers["X-Agent-Version"] == "0.4.7"


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
async def test_sequence_is_time_based(client: PerunChatClient) -> None:
    """A fresh client starts above other clients sharing agent/runtime."""
    assert client._seq >= int(time.time() * 1000) - 5_000
    mock_ws = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws.__aexit__ = AsyncMock(return_value=False)

    async def fake_connect(*args, **kwargs):
        return mock_ws

    client._seq = 1
    with patch("websockets.connect", side_effect=fake_connect):
        await client.connect()
    assert client._seq >= int(time.time() * 1000) - 5_000


@pytest.mark.asyncio
async def test_send_frame_uses_requested_content_type(client: PerunChatClient) -> None:
    mock_ws = AsyncMock()
    client.ws = mock_ws
    client._seq = 0

    frame = await client.send_frame(
        room_id="grp:belle-core:demo",
        room_type=RoomType.GROUP,
        content_type="application/perun-execution",
        payload={"op": "claim_execution", "proposal_id": "prop_x"},
        recipient_id="peruntheoverseer",
    )

    assert frame["content_type"] == "application/perun-execution"
    assert frame["recipient_id"] == "peruntheoverseer"
    assert frame["payload"]["op"] == "claim_execution"
    sent = json.loads(mock_ws.send.call_args.args[0])
    assert sent["content_type"] == "application/perun-execution"
    assert sent["recipient_id"] == "peruntheoverseer"
    assert sent["sender"]["agent_id"] == "test-agent"


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


@pytest.mark.asyncio
async def test_attempt_reconnect_does_not_await_itself() -> None:
    """Reconnect runs inside the listener task; it must not await that task."""
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="test-agent",
        runtime="perun",
        reconnect=True,
    )
    client.ws = AsyncMock()
    # Simulate being called from inside the listener task.
    client._listen_task = asyncio.current_task()

    reconnect_calls: list[int] = []

    async def fake_connect() -> None:
        reconnect_calls.append(1)
        # Avoid starting a real listener during this test.
        client._listen_task = None

    with patch.object(client, "connect", side_effect=fake_connect):
        with patch("perunchat.client.asyncio.sleep", new=AsyncMock()):
            await asyncio.wait_for(client._attempt_reconnect(), timeout=2)

    assert reconnect_calls == [1]
    assert client._listen_task is None


@pytest.mark.asyncio
async def test_reconnect_rejoins_private_rooms() -> None:
    """After a reconnect, the client must rejoin private rooms as PRIVATE."""
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="test-agent",
        runtime="perun",
        reconnect=True,
    )
    client._joined_rooms = {"pub:global:general", "grp:project:room", "prv:alice:bob"}

    join_calls: list = []

    async def mock_connect(*args, **kwargs):
        mock_ws = AsyncMock()
        return mock_ws

    async def mock_join(room_id: str, room_type: RoomType) -> None:
        join_calls.append((room_id, room_type))

    with patch("websockets.connect", side_effect=mock_connect):
        with patch.object(client, "join_room", side_effect=mock_join):
            await client.connect()

    assert ("pub:global:general", RoomType.PUBLIC) in join_calls
    assert ("grp:project:room", RoomType.GROUP) in join_calls
    assert ("prv:alice:bob", RoomType.PRIVATE) in join_calls

    await client.disconnect()
