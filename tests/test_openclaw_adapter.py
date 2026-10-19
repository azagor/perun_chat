"""Tests for the OpenClaw adapter."""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from perunchat.openclaw_adapter import (
    OpenClawNodeContext,
    PerunChatOpenClawPlugin,
    make_openclaw_plugin,
)


@pytest.fixture
def context() -> OpenClawNodeContext:
    return OpenClawNodeContext(
        node_id="openclaw-node-01",
        runtime="openclaw",
        api_key="prn_test_key",
        endpoint="wss://peruninthemachine.com/v1/chat/ws",
        rooms=["grp:belle-core:demo"],
    )


@pytest.fixture
def plugin(context: OpenClawNodeContext) -> PerunChatOpenClawPlugin:
    emitted: List[Dict[str, Any]] = []
    p = PerunChatOpenClawPlugin(context, transport=emitted.append)
    return p


def test_plugin_metadata(context: OpenClawNodeContext) -> None:
    plugin = PerunChatOpenClawPlugin(context)
    assert plugin.name == "perunchat_openclaw"
    assert plugin.version == "0.1.0"


def test_hooks_exposed(context: OpenClawNodeContext) -> None:
    plugin = PerunChatOpenClawPlugin(context)
    hooks = plugin.hooks()
    assert "agent.chat.send" in hooks
    assert "agent.chat.join" in hooks
    assert "agent.chat.leave" in hooks
    assert "agent.propose" in hooks
    assert "agent.chat.on_message" in hooks


def test_send_message(plugin: PerunChatOpenClawPlugin) -> None:
    frame = plugin.send_message(
        room_id="grp:belle-core:demo",
        room_type="GROUP",
        message="Hello from OpenClaw",
    )
    assert frame["type"] == "CHAT"
    assert frame["room_id"] == "grp:belle-core:demo"
    assert frame["content_type"] == "text/plain"
    assert frame["payload"] == "Hello from OpenClaw"
    assert frame["sender"]["agent_id"] == "openclaw-node-01"
    assert frame["sender"]["runtime"] == "openclaw"


def test_join_and_leave_room(plugin: PerunChatOpenClawPlugin) -> None:
    join_frame = plugin.join_room("grp:belle-core:demo")
    assert join_frame["type"] == "ROOM_JOIN"
    assert join_frame["room_id"] == "grp:belle-core:demo"

    leave_frame = plugin.leave_room("grp:belle-core:demo")
    assert leave_frame["type"] == "ROOM_LEAVE"


def test_propose_action(plugin: PerunChatOpenClawPlugin) -> None:
    frame = plugin.propose_action(
        room_id="grp:belle-core:demo",
        risk_tier="TIER_3_HIGH_IMPACT",
        action_domain="SHELL",
        description="Restart worker pool",
        execution_details={"command": "systemctl restart worker"},
        justification="Relieve memory pressure",
    )
    assert frame["content_type"] == "application/perun-proposal"
    payload = frame["payload"]
    assert payload["proposal_type"] == "PERUN_ACTION_PROPOSAL"
    assert payload["risk_tier"] == "TIER_3_HIGH_IMPACT"
    assert payload["provenance"]["instigator"] == "openclaw-node-01"


def test_inbound_dispatch(plugin: PerunChatOpenClawPlugin) -> None:
    received: List[Dict[str, Any]] = []
    plugin.register_message_handler(received.append)
    plugin.dispatch_inbound({"payload": "ping"})
    assert len(received) == 1
    assert received[0]["payload"] == "ping"


def test_factory() -> None:
    config = {
        "node_id": "oc-02",
        "api_key": "prn_test_key",
        "endpoint": "wss://peruninthemachine.com/v1/chat/ws",
        "rooms": ["pub:global:alerts"],
    }
    plugin = make_openclaw_plugin(config)
    assert plugin.context.node_id == "oc-02"
    assert "pub:global:alerts" in plugin.context.rooms
