"""Conformance tests for non-Perun adapter frame shapes and executor wiring."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from typing import Any, Dict, List
from unittest.mock import AsyncMock, Mock, patch

import pytest

os.environ.setdefault("PERUNCHAT_ENV", "test")

from perunchat.client import PerunChatClient, RiskTier, RoomType
from perunchat.hermes_adapter import HERMES_PERUNCHAT_TOOLS, execute_hermes_tool
from perunchat.openclaw_adapter import make_openclaw_plugin
from perunchat.zeroclaw_client import ZeroClawClient


@pytest.mark.asyncio
async def test_python_sdk_connect_headers_match_websocket_version() -> None:
    """The SDK must use the correct header kwarg for the installed websockets."""
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="sdk-agent",
        runtime="custom",
        version="1.2.3",
    )
    mock_ws = AsyncMock()
    mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
    mock_ws.__aexit__ = AsyncMock(return_value=False)

    async def fake_connect(*args, **kwargs):
        return mock_ws

    with patch("perunchat.client.websockets.connect", side_effect=fake_connect) as connect_mock:
        await client.connect()

    connect_mock.assert_called_once()
    _, kwargs = connect_mock.call_args
    headers = kwargs.get("additional_headers") or kwargs.get("extra_headers")
    assert headers["Authorization"] == "Bearer prn_live_secret"
    assert headers["X-Agent-ID"] == "sdk-agent"
    assert headers["X-Agent-Runtime"] == "custom"
    assert headers["X-Agent-Version"] == "1.2.3"
    # Headers must be delivered under the kwarg the SDK selected.
    assert "additional_headers" in kwargs or "extra_headers" in kwargs
    await client.disconnect()


@pytest.mark.asyncio
async def test_python_sdk_self_echo_is_filtered() -> None:
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="sdk-agent",
        runtime="custom",
    )

    class FakeWS:
        def __init__(self, messages):
            self._messages = messages
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

        async def close(self):
            self.closed = True

    received: List[Dict[str, Any]] = []
    client.on_message(received.append)
    fake = FakeWS([json.dumps({"sender": {"agent_id": "sdk-agent"}, "payload": "echo"})])
    client.ws = fake
    client._listen_task = asyncio.create_task(client._listen_loop())
    await asyncio.sleep(0.05)
    client._closed = True
    await client.disconnect()
    assert not received


@pytest.mark.asyncio
async def test_python_sdk_send_text_and_proposal_frames() -> None:
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="sdk-agent",
        runtime="custom",
    )
    mock_ws = AsyncMock()
    client.ws = mock_ws
    client._seq = 0

    text_frame = await client.send_text(
        "grp:conform:room", RoomType.GROUP, "hello"
    )
    assert text_frame["content_type"] == "text/plain"
    assert text_frame["sequence_number"] == 1
    assert text_frame["sender"]["runtime"] == "custom"

    prop_frame = await client.submit_proposal(
        room_id="grp:conform:room",
        risk_tier=RiskTier.TIER_2_LOW_IMPACT,
        action_domain="SHELL",
        description="x",
        execution_details={"command": "echo hi"},
        justification="y",
    )
    assert prop_frame["content_type"] == "application/perun-proposal"
    assert prop_frame["sequence_number"] == 2
    payload = prop_frame["payload"]
    assert payload["proposal_type"] == "PERUN_ACTION_PROPOSAL"
    assert payload["risk_tier"] == "TIER_2_LOW_IMPACT"
    assert payload["execution_details"]["command"] == "echo hi"


@pytest.mark.asyncio
async def test_python_sdk_stage_proposal_helper() -> None:
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="sdk-agent",
        runtime="custom",
    )
    mock_ws = AsyncMock()
    client.ws = mock_ws
    pid = await client.stage_proposal(
        room_id="grp:conform:room",
        risk_tier=RiskTier.TIER_2_LOW_IMPACT,
        action_domain="API",
        description="desc",
        execution_details={"file_path": "a"},
        justification="because",
    )
    assert pid.startswith("prop_")
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["content_type"] == "application/perun-proposal"
    assert sent["payload"]["proposal_id"] == pid
    assert "approval_token" not in sent["payload"]


def test_zeroclaw_sequence_increments() -> None:
    zc = ZeroClawClient("prn_live_secret", "zc-agent")
    mock_ws = Mock()
    zc.ws = mock_ws
    zc.send_text("grp:conform:room", "GROUP", "first")
    zc.send_text("grp:conform:room", "GROUP", "second")
    m1 = json.loads(mock_ws.send.call_args_list[0][0][0])
    m2 = json.loads(mock_ws.send.call_args_list[1][0][0])
    assert m1["sequence_number"] == 1
    assert m2["sequence_number"] == 2
    assert m1["sender"]["runtime"] == "zeroclaw"
    assert "version" in m1["sender"]


def test_openclaw_plugin_has_required_frame_fields() -> None:
    plugin = make_openclaw_plugin(
        {
            "node_id": "oc-agent",
            "api_key": "prn_live_x",
            "endpoint": "wss://peruninthemachine.com/v1/chat/ws",
            "rooms": ["grp:conform:room"],
        }
    )
    emitted: List[Dict[str, Any]] = []
    plugin.transport = emitted.append
    plugin.join_room("grp:conform:room", "GROUP")
    plugin.send_message("grp:conform:room", "GROUP", "hi")
    proposal = plugin.propose_action(
        "grp:conform:room",
        "TIER_2_LOW_IMPACT",
        "FILESYSTEM",
        "Update docs",
        {"file_path": "README.md", "diff": "+line"},
        "Docs refresh",
    )
    assert len(emitted) == 3
    join, msg, prop = emitted
    assert "id" in msg
    assert "sequence_number" in msg
    assert "timestamp" in msg
    assert msg["sender"]["version"] == "0.1.0"
    assert prop["content_type"] == "application/perun-proposal"
    assert prop["payload"]["proposal_type"] == "PERUN_ACTION_PROPOSAL"
    assert "sequence_number" not in join  # control frames omit sequence


def test_hermes_tool_definitions_match_wire() -> None:
    names = {t["function"]["name"] for t in HERMES_PERUNCHAT_TOOLS}
    assert "perunchat_post_message" in names
    assert "perunchat_propose_state_action" in names
    propose = next(
        t for t in HERMES_PERUNCHAT_TOOLS if t["function"]["name"] == "perunchat_propose_state_action"
    )
    props = propose["function"]["parameters"]["properties"]
    assert "execution_details" in props
    assert "TIER_0_READ_ONLY" in props["risk_tier"]["enum"]


@pytest.mark.asyncio
async def test_hermes_executor_sends_message() -> None:
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="hermes-agent",
        runtime="hermes",
    )
    mock_ws = AsyncMock()
    client.ws = mock_ws
    result = await execute_hermes_tool(
        client,
        "perunchat_post_message",
        {
            "room_id": "grp:conform:room",
            "room_type": "GROUP",
            "message": "hello from hermes",
        },
    )
    assert "Message sent" in result
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["content_type"] == "text/plain"
    assert sent["payload"] == "hello from hermes"
    assert sent["room_id"] == "grp:conform:room"


@pytest.mark.asyncio
async def test_hermes_executor_stages_proposal() -> None:
    client = PerunChatClient(
        endpoint="ws://127.0.0.1:8765/v1/chat/ws",
        api_key="prn_live_secret",
        agent_id="hermes-agent",
        runtime="hermes",
    )
    mock_ws = AsyncMock()
    client.ws = mock_ws
    result = await execute_hermes_tool(
        client,
        "perunchat_propose_state_action",
        {
            "room_id": "grp:conform:room",
            "risk_tier": "TIER_3_HIGH_IMPACT",
            "action_domain": "SHELL",
            "description": "Restart worker",
            "execution_details": {"command": "systemctl restart worker"},
            "justification": "Memory pressure",
        },
    )
    assert "Action proposal emitted" in result
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["content_type"] == "application/perun-proposal"
    assert sent["payload"]["risk_tier"] == "TIER_3_HIGH_IMPACT"
