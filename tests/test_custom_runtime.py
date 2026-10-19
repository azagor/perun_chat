"""Tests for the environment-agnostic custom runtime adapter."""

from __future__ import annotations

from perunchat.custom_runtime import InMemoryTransport, PerunChatRuntime


def test_custom_runtime_send_text() -> None:
    transport = InMemoryTransport()
    runtime = PerunChatRuntime(
        agent_id="custom-bot-1",
        runtime="my-custom-runtime",
        transport=transport,
    )
    runtime.join_room("grp:tenant:general")
    frame = runtime.send_text(
        room_id="grp:tenant:general",
        room_type="GROUP",
        text="Hello from a custom runtime",
    )

    assert len(transport.outbox) == 2
    join, chat = transport.outbox
    assert join["type"] == "ROOM_JOIN"
    assert chat["payload"] == "Hello from a custom runtime"
    assert chat["sender"]["runtime"] == "my-custom-runtime"
    assert frame["sequence_number"] == 1


def test_custom_runtime_propose_action() -> None:
    transport = InMemoryTransport()
    runtime = PerunChatRuntime(agent_id="custom-bot-2", runtime="custom", transport=transport)
    frame = runtime.propose_action(
        room_id="grp:tenant:general",
        risk_tier="TIER_2_LOW_IMPACT",
        action_domain="FILESYSTEM",
        description="Update docs",
        execution_details={"file_path": "README.md", "diff": "+line"},
        justification="Docs refresh",
    )

    assert frame["content_type"] == "application/perun-proposal"
    payload = frame["payload"]
    assert payload["proposal_type"] == "PERUN_ACTION_PROPOSAL"
    assert payload["risk_tier"] == "TIER_2_LOW_IMPACT"


def test_custom_runtime_inbound_listener() -> None:
    transport = InMemoryTransport()
    runtime = PerunChatRuntime(agent_id="custom-bot-3", runtime="custom", transport=transport)

    received = []
    runtime.on_message(received.append)
    transport.inject({"payload": "ping"})
    runtime.poll()

    assert len(received) == 1
    assert received[0]["payload"] == "ping"
