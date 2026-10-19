"""Tests for Pydantic models."""

from __future__ import annotations

import pytest

from perunchat.client import RiskTier
from perunchat.models import ActionProposal, ChatFrame, RoomType, Runtime


def test_chat_frame_accepts_custom_runtime() -> None:
    frame = ChatFrame(
        room_type=RoomType.GROUP,
        room_id="grp:tenant:room",
        sequence_number=1,
        sender={"agent_id": "bot", "runtime": "my-custom-runtime", "version": "1.0"},
        content_type="text/plain",
        payload="hello",
    )
    assert frame.sender.runtime == "my-custom-runtime"


def test_chat_frame_rejects_invalid_room_id() -> None:
    with pytest.raises(ValueError):
        ChatFrame(
            room_type=RoomType.GROUP,
            room_id="not-a-room",
            sequence_number=1,
            sender={"agent_id": "bot", "runtime": Runtime.PERUN},
            content_type="text/plain",
            payload="hello",
        )


def test_risk_tier_validation() -> None:
    proposal = ActionProposal(
        risk_tier="TIER_3_HIGH_IMPACT",
        action_domain="FILESYSTEM",
        description="Update config",
        provenance={"instigator": "bot", "justification": "needed"},
    )
    assert proposal.risk_tier == "TIER_3_HIGH_IMPACT"

    with pytest.raises(ValueError):
        ActionProposal(
            risk_tier="TIER_99",
            action_domain="FILESYSTEM",
            description="bad",
            provenance={"instigator": "bot", "justification": "x"},
        )
