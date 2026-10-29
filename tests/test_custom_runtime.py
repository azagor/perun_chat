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


def test_custom_runtime_stage_and_resubmit_with_token() -> None:
    transport = InMemoryTransport()
    runtime = PerunChatRuntime(agent_id="custom-bot-4", runtime="custom", transport=transport)

    staged = runtime.stage_proposal(
        room_id="grp:tenant:general",
        risk_tier="TIER_3_HIGH_IMPACT",
        action_domain="SHELL",
        description="Restart worker",
        execution_details={"command": "systemctl restart worker"},
        justification="Routine restart",
        proposal_id="prop_custom_001",
    )
    assert staged["content_type"] == "application/perun-proposal"
    assert staged["payload"]["proposal_id"] == "prop_custom_001"
    assert "approval_token" not in staged["payload"]

    approval_token = {
        "token": "hmac-token-value",
        "proposal_id": "prop_custom_001",
        "expiry_epoch": 1739818980,
        "nonce": "nonce-123",
    }
    resent = runtime.resubmit_with_token(staged, approval_token)
    assert resent["payload"]["proposal_id"] == "prop_custom_001"
    assert resent["payload"]["approval_token"] == "hmac-token-value"
    assert resent["payload"]["approval_expiry_epoch"] == 1739818980
    assert resent["payload"]["approval_nonce"] == "nonce-123"
    assert resent["sequence_number"] == staged["sequence_number"] + 1
    assert resent["id"] != staged["id"]
