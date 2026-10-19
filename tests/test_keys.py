"""Tests for API key lifecycle management."""

from __future__ import annotations

import pytest

from perunchat.keys import DuplicateAgentIdError, InvalidAgentIdError, KeyStore


def _store() -> KeyStore:
    return KeyStore(":memory:")


def test_create_key_returns_plaintext_once() -> None:
    store = _store()
    raw, record = store.create(
        owner_id="owner-1",
        agent_id="agent-1",
        supported_runtime="perun",
        scopes=["grp:belle-core:demo"],
        live=True,
    )
    assert raw.startswith("prn_live_")
    assert record.key_hash != raw
    assert record.key_prefix == raw[: len("prn_live_") + 8]
    assert not record.is_revoked


def test_authenticate_valid_key() -> None:
    store = _store()
    raw, _ = store.create(
        owner_id="owner-1",
        agent_id="agent-1",
        supported_runtime="perun",
        scopes=["grp:belle-core:demo"],
    )
    found = store.authenticate(raw)
    assert found is not None
    assert found.agent_id == "agent-1"


def test_authenticate_rejects_revoked_key() -> None:
    store = _store()
    raw, record = store.create(
        owner_id="owner-1",
        agent_id="agent-1",
        supported_runtime="perun",
        scopes=["grp:belle-core:demo"],
    )
    store.revoke(record.id)
    assert store.authenticate(raw) is None


def test_update_scopes() -> None:
    store = _store()
    _, record = store.create(
        owner_id="owner-1",
        agent_id="agent-1",
        supported_runtime="perun",
        scopes=["grp:a"],
    )
    updated = store.update_scopes(record.id, ["grp:a", "grp:b"])
    assert updated is not None
    assert updated.scopes == ["grp:a", "grp:b"]


def test_duplicate_agent_id_rejected() -> None:
    store = _store()
    store.create(
        owner_id="owner-1",
        agent_id="agent-unique",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    with pytest.raises(DuplicateAgentIdError):
        store.create(
            owner_id="owner-2",
            agent_id="agent-unique",
            supported_runtime="openclaw",
            scopes=["pub:global:*"],
        )


def test_revoke_frees_agent_id() -> None:
    store = _store()
    raw, record = store.create(
        owner_id="owner-1",
        agent_id="agent-unique",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    assert store.authenticate(raw) is not None
    store.revoke(record.id)
    assert store.authenticate(raw) is None

    raw2, record2 = store.create(
        owner_id="owner-1",
        agent_id="agent-unique",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    assert record2.agent_id == "agent-unique"
    assert raw2 != raw


def test_case_insensitive_uniqueness() -> None:
    store = _store()
    store.create(
        owner_id="owner-1",
        agent_id="Agent-One",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    with pytest.raises(DuplicateAgentIdError):
        store.create(
            owner_id="owner-2",
            agent_id="agent-one",
            supported_runtime="hermes",
            scopes=["pub:global:*"],
        )


@pytest.mark.parametrize(
    "agent_id",
    ["ab", "a" * 65, "bad id", "admin", "system", "gateway", "PerunInTheMachine"],
)
def test_invalid_or_reserved_agent_id_rejected(agent_id: str) -> None:
    store = _store()
    with pytest.raises(InvalidAgentIdError):
        store.create(
            owner_id="owner-1",
            agent_id=agent_id,
            supported_runtime="perun",
            scopes=["pub:global:*"],
        )


def test_is_agent_id_available() -> None:
    store = _store()
    assert store.is_agent_id_available("free-agent") is True
    store.create(
        owner_id="owner-1",
        agent_id="free-agent",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    assert store.is_agent_id_available("free-agent") is False
    assert store.is_agent_id_available("admin") is False
