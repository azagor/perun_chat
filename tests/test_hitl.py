"""Tests for HITL approval token engine."""

from __future__ import annotations

import time
from typing import Any

import pytest
from perunchat.hitl import ApprovalStore, HITLConfig, HITLTokenEngine


def test_issue_and_validate_token() -> None:
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    details = {"command": "echo hello", "file_path": None}
    issued = engine.issue("prop_123", details)

    assert issued["expiry_epoch"] == 0
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details=details,
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert ok
    assert reason is None


def test_reject_replayed_nonce() -> None:
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})
    engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "nonce_replay"


def test_token_does_not_expire() -> None:
    """Tokens have no expiry; an old issued token still validates."""
    engine = HITLTokenEngine(
        HITLConfig(secret_key=b"test-secret", token_ttl_seconds=0)
    )
    issued = engine.issue("prop_123", {})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert ok
    assert reason is None


def test_consumed_nonce_remains_used() -> None:
    """A consumed nonce must never become reusable, even far in the future."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})
    engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert engine.store.consume(issued["nonce"], now=time.time() + 10_000_000) is False


def test_consumed_nonce_persists_across_restarts() -> None:
    """Replay protection must survive store recreation."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    config = HITLConfig(secret_key=b"test-secret", db_path=tmp_path)
    engine1 = HITLTokenEngine(config)
    issued = engine1.issue("prop_123", {})
    assert engine1.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    ) == (True, None)

    engine2 = HITLTokenEngine(config)
    ok, reason = engine2.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert (ok, reason) == (False, "nonce_replay")

    engine1.close()
    engine2.close()

    import os

    os.unlink(tmp_path)


def test_store_busy_is_reported_as_a_distinct_reason(monkeypatch) -> None:
    """A locked nonce store must not be reported as a replay or crash the call."""
    from perunchat.hitl import ApprovalStoreBusy

    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})

    def busy(*args, **kwargs):
        raise ApprovalStoreBusy("database is locked")

    monkeypatch.setattr(engine.store, "consume", busy)
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert (ok, reason) == (False, "store_busy")


def test_store_full_is_reported_as_a_distinct_reason() -> None:
    """The append-only nonce store fails closed instead of evicting nonces."""
    engine = HITLTokenEngine(
        HITLConfig(secret_key=b"test-secret", max_consumed_nonces=1)
    )
    first = engine.issue("prop_first", {})
    assert engine.validate(
        token=first["token"],
        proposal_id=first["proposal_id"],
        execution_details={},
        expiry_epoch=first["expiry_epoch"],
        nonce=first["nonce"],
    ) == (True, None)

    second = engine.issue("prop_second", {})
    ok, reason = engine.validate(
        token=second["token"],
        proposal_id=second["proposal_id"],
        execution_details={},
        expiry_epoch=second["expiry_epoch"],
        nonce=second["nonce"],
    )
    assert (ok, reason) == (False, "store_full")
    # The first nonce was not evicted.
    assert engine.store.count() == 1


def test_prune_before_only_after_key_rotation() -> None:
    """Nonces older than a cutoff can be pruned explicitly."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    old = engine.issue("prop_old", {})
    new = engine.issue("prop_new", {})
    engine.store.consume(old["nonce"], now=100)
    engine.store.consume(new["nonce"], now=200)
    assert engine.store.count() == 2

    assert engine.store.prune_before(150) == 1
    assert engine.store.count() == 1
    assert engine.store.consume(new["nonce"], now=300) is False


def test_bad_signature_does_not_burn_nonce() -> None:
    """An invalid signature must not consume the single-use nonce."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    details = {"command": "x"}
    issued = engine.issue("prop_123", details)
    ok, reason = engine.validate(
        token="0" * 64,
        proposal_id=issued["proposal_id"],
        execution_details=details,
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert (ok, reason) == (False, "invalid_signature")
    # The real token should still validate because the nonce was not consumed.
    ok2, reason2 = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details=details,
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert (ok2, reason2) == (True, None)


def test_action_hash_ignores_nested_key_order() -> None:
    """Execution details with identical nested content hash the same."""
    H = HITLTokenEngine.action_hash
    assert H({"a": {"x": 1, "y": 2}}) == H({"a": {"y": 2, "x": 1}})


def test_action_hash_distinguishes_types() -> None:
    """JSON types must not collapse during canonicalisation."""
    H = HITLTokenEngine.action_hash
    assert H({"a": 1}) != H({"a": "1"})


@pytest.mark.parametrize("bad", [123, None, "é", b"x"])
def test_non_str_token_is_rejected_not_raised(bad: Any) -> None:
    """Non-string tokens must return invalid_signature, not raise."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})
    ok, reason = engine.validate(
        token=bad,  # type: ignore[arg-type]
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert (ok, reason) == (False, "invalid_signature")


@pytest.mark.parametrize(
    "bad_expiry",
    ["x", None, [], {}, float("nan")],
)
def test_malformed_expiry_is_rejected_not_raised(bad_expiry: Any) -> None:
    """Malformed expiry_epoch values must return invalid_signature, not raise."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=bad_expiry,
        nonce=issued["nonce"],
    )
    assert ok is False
    assert reason == "invalid_signature"


@pytest.mark.parametrize("bad", [5, None, ["n"], {"n": 1}])
def test_non_str_nonce_is_rejected_not_raised(bad: Any) -> None:
    """Non-string nonces must return invalid_signature, not raise."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=bad,  # type: ignore[arg-type]
    )
    assert ok is False
    assert reason == "invalid_signature"


@pytest.mark.parametrize("bad", ["x", [1], None])
def test_non_dict_execution_details_is_rejected_not_raised(bad: Any) -> None:
    """Execution details must be a dict."""
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    issued = engine.issue("prop_123", {})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details=bad,  # type: ignore[arg-type]
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert ok is False
    assert reason == "invalid_signature"
