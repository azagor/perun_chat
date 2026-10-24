"""Tests for the PerunChat security pipeline."""

from __future__ import annotations

import time

import pytest

from perunchat.security import (
    DLPScanner,
    RateLimiter,
    SequenceValidator,
    SecurityPipeline,
    TurnCounter,
    generate_api_key,
)


def test_generate_api_key() -> None:
    key = generate_api_key("prn_live_")
    assert key.startswith("prn_live_")
    assert len(key) > len("prn_live_")
    with pytest.raises(ValueError):
        generate_api_key("prn_test_")


def test_dlp_redacts_perun_token() -> None:
    scanner = DLPScanner()
    text = "My token is prn_live_7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f"
    redacted, alerts = scanner.scan(text)
    assert "[REDACTED_SECRET]" in redacted
    assert any("perun_token" in a for a in alerts)


def test_dlp_redacts_private_key() -> None:
    scanner = DLPScanner()
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    redacted, alerts = scanner.scan(text)
    assert "[REDACTED_SECRET]" in redacted
    assert any("ed25519_private_key" in a for a in alerts)


async def test_rate_limiter_allows_burst_then_blocks() -> None:
    limiter = RateLimiter(capacity=3, refill_per_second=10)
    assert await limiter.allow("agent-1")
    assert await limiter.allow("agent-1")
    assert await limiter.allow("agent-1")
    assert not await limiter.allow("agent-1")


def test_sequence_validator_rejects_non_monotonic() -> None:
    seq = SequenceValidator()
    ok, _ = seq.validate("agent-1", "custom", 1, int(time.time() * 1000))
    assert ok
    ok, reason = seq.validate("agent-1", "custom", 1, int(time.time() * 1000))
    assert not ok
    assert reason == "non_monotonic_sequence"


def test_sequence_validator_rejects_timestamp_skew() -> None:
    seq = SequenceValidator()
    old = int(time.time() * 1000) - 200_000
    ok, reason = seq.validate("agent-1", "custom", 1, old)
    assert not ok
    assert reason == "timestamp_skew"


def test_sequence_validator_rejects_duplicate_nonce() -> None:
    seq = SequenceValidator()
    now = int(time.time() * 1000)
    ok, _ = seq.validate("agent-1", "custom", 1, now, nonce="abc")
    assert ok
    ok, reason = seq.validate("agent-1", "custom", 2, now, nonce="abc")
    assert not ok
    assert reason == "duplicate_nonce"


def test_sequence_validator_keeps_runtimes_separate() -> None:
    seq = SequenceValidator()
    now = int(time.time() * 1000)
    ok, _ = seq.validate("agent-1", "perun", 5, now)
    assert ok
    # A different runtime for the same agent_id should have its own stream.
    ok, _ = seq.validate("agent-1", "custom", 1, now)
    assert ok
    ok, reason = seq.validate("agent-1", "custom", 1, now)
    assert not ok
    assert reason == "non_monotonic_sequence"


def test_turn_counter_locks_after_eight() -> None:
    turns = TurnCounter()
    for i in range(1, 8):
        count, locked = turns.increment("thrd-1")
        assert not locked
    count, locked = turns.increment("thrd-1")
    assert count == 8
    assert locked


def test_turn_counter_resets_on_operator_input() -> None:
    turns = TurnCounter()
    for _ in range(5):
        turns.increment("thrd-2")
    count, locked = turns.increment("thrd-2", sender_runtime="operator")
    assert count == 0
    assert not locked


def test_turn_counter_resets_on_custom_runtime() -> None:
    """A dashboard/human client sends runtime=custom and must clear the lock."""
    turns = TurnCounter()
    for _ in range(8):
        turns.increment("thrd-3", agent_id="agent-a")
    assert turns.is_locked("thrd-3", "agent-a")
    count, locked = turns.increment("thrd-3", sender_runtime="custom", agent_id="agent-a")
    assert count == 0
    assert not locked
    assert not turns.is_locked("thrd-3", "agent-a")


def test_turn_counter_resets_on_agent_specific_operator_runtime() -> None:
    """Agent-scoped operator runtimes (e.g. 'perun-agent-operator') must reset
    the autonomous turn lock just like the generic 'operator' runtime."""
    turns = TurnCounter()
    for _ in range(8):
        turns.increment("thrd-operator-norm", agent_id="perun-agent")
    assert turns.is_locked("thrd-operator-norm", "perun-agent")
    count, locked = turns.increment(
        "thrd-operator-norm", sender_runtime="perun-agent-operator", agent_id="perun-agent"
    )
    assert count == 0
    assert not locked
    assert not turns.is_locked("thrd-operator-norm", "perun-agent")


def test_turn_counter_is_per_agent() -> None:
    """One busy agent must not lock the thread for another participant."""
    turns = TurnCounter()
    for _ in range(8):
        turns.increment("thrd-4", agent_id="agent-a")
    assert turns.is_locked("thrd-4", "agent-a")
    # A different agent on the same thread is unaffected.
    count, locked = turns.increment("thrd-4", agent_id="agent-b")
    assert count == 1
    assert not locked
    assert not turns.is_locked("thrd-4", "agent-b")


def test_turn_counter_prunes_stale_entries() -> None:
    """Old entries are evicted so memory does not grow without bound."""
    turns = TurnCounter()
    turns.PRUNE_AGE_SECONDS = 0.05
    turns.increment("thrd-5", agent_id="agent-a")
    assert len(turns._last_seen) == 1
    import time
    time.sleep(0.06)
    removed = turns.prune()
    assert removed == 1
    assert len(turns._last_seen) == 0
    assert not turns.is_locked("thrd-5", "agent-a")


def test_security_pipeline_scan_egress() -> None:
    pipeline = SecurityPipeline()
    frame = {"payload": "secret prn_live_deadbeef"}
    safe = pipeline.scan_egress(frame)
    assert "[REDACTED_SECRET]" in safe["payload"]


async def test_auth_rate_limiter_blocks_after_five_failures() -> None:
    pipeline = SecurityPipeline()
    for _ in range(5):
        assert await pipeline.check_auth_allowed("ip-1")
        await pipeline.record_auth_failure("ip-1")
    assert not await pipeline.check_auth_allowed("ip-1")
    # Other identifiers unaffected
    assert await pipeline.check_auth_allowed("ip-2")
