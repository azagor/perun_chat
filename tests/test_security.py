"""Tests for the PerunChat security pipeline."""

from __future__ import annotations

import asyncio
import time

import pytest

from perunchat.security import (
    AuthRateLimiter,
    DLPScanner,
    RateLimiter,
    SequenceValidator,
    SecurityPipeline,
    TokenBucket,
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


def test_sequence_validator_separates_client_streams() -> None:
    seq = SequenceValidator()
    now = int(time.time() * 1000)
    ok, _ = seq.validate("agent-1", "mcp", 1, now, stream_id="bridge")
    assert ok
    # Same agent/runtime but a different client stream is independent.
    ok, _ = seq.validate("agent-1", "mcp", 1, now, stream_id="chat")
    assert ok
    ok, reason = seq.validate("agent-1", "mcp", 1, now, stream_id="bridge")
    assert not ok
    assert reason == "non_monotonic_sequence"


def test_sequence_validator_caps_stream_table(monkeypatch) -> None:
    seq = SequenceValidator()
    monkeypatch.setattr(seq, "MAX_STREAMS", 2)
    now = int(time.time() * 1000)
    for index in range(3):
        ok, _ = seq.validate("agent-1", "mcp", 1, now, stream_id=f"s{index}")
        assert ok
    assert len(seq._last_seq) == 2
    # The oldest stream was evicted and can start a fresh sequence.
    ok, _ = seq.validate("agent-1", "mcp", 1, now, stream_id="s0")
    assert ok


def test_turn_counter_locks_after_eight() -> None:
    turns = TurnCounter()
    # With regenerating budget, burst limit is the main protection
    for i in range(1, turns.BURST_LIMIT):
        count, locked = turns.increment("thrd-1")
        assert not locked
    count, locked = turns.increment("thrd-1")
    assert count >= turns.BURST_LIMIT or locked
    assert locked


def test_turn_counter_resets_on_operator_input() -> None:
    turns = TurnCounter()
    for _ in range(5):
        turns.increment("thrd-2")
    count, locked = turns.increment("thrd-2", sender_runtime="operator")
    assert count == turns.MAX_CREDITS
    assert not locked


def test_turn_counter_resets_on_custom_runtime() -> None:
    """A dashboard/human client sends runtime=custom and must clear the lock."""
    turns = TurnCounter()
    for _ in range(turns.BURST_LIMIT):
        turns.increment("thrd-3", agent_id="agent-a")
    assert turns.is_locked("thrd-3", "agent-a")
    count, locked = turns.increment("thrd-3", sender_runtime="custom", agent_id="agent-a")
    assert count == turns.MAX_CREDITS
    assert not locked
    assert not turns.is_locked("thrd-3", "agent-a")


def test_turn_counter_resets_on_agent_specific_operator_runtime() -> None:
    """Agent-scoped operator runtimes (e.g. 'perun-agent-operator') must reset
    the autonomous turn lock just like the generic 'operator' runtime."""
    turns = TurnCounter()
    for _ in range(turns.BURST_LIMIT):
        turns.increment("thrd-operator-norm", agent_id="perun-agent")
    assert turns.is_locked("thrd-operator-norm", "perun-agent")
    count, locked = turns.increment(
        "thrd-operator-norm", sender_runtime="perun-agent-operator", agent_id="perun-agent"
    )
    assert count == turns.MAX_CREDITS
    assert not locked
    assert not turns.is_locked("thrd-operator-norm", "perun-agent")


def test_turn_counter_is_per_agent() -> None:
    """One busy agent must not lock the thread for another participant."""
    turns = TurnCounter()
    for _ in range(turns.BURST_LIMIT):
        turns.increment("thrd-4", agent_id="agent-a")
    assert turns.is_locked("thrd-4", "agent-a")
    # A different agent on the same thread is unaffected.
    count, locked = turns.increment("thrd-4", agent_id="agent-b")
    assert count < turns.MAX_CREDITS
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



@pytest.mark.asyncio
async def test_auth_limiter_attempt_is_atomic():
    limiter = AuthRateLimiter(capacity=5, window_seconds=60)
    results = await asyncio.gather(*(limiter.attempt("x") for _ in range(50)))
    assert sum(1 for allowed in results if allowed) == 5


@pytest.mark.asyncio
async def test_auth_limiter_prunes_expired_keys(monkeypatch):
    limiter = AuthRateLimiter(capacity=5, window_seconds=1)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    await limiter.record_failure("old")
    now += 10
    await limiter.attempt("new")
    assert "old" not in limiter._failures


# --- DLP must never corrupt technical text ---------------------------------

def test_dlp_preserves_backlog_filenames() -> None:
    """Regression: the entropy heuristic rewrote `HARDENING_BACKLOG_PART3`."""
    scanner = DLPScanner()
    text = "Create docs/HARDENING_BACKLOG_PART3.md continuing PART2 (items 12-16)."
    scanned, alerts = scanner.scan(text)
    assert scanned == text
    assert alerts == []


def test_dlp_entropy_is_detect_only_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PERUNCHAT_DLP_ENTROPY_REDACT", raising=False)
    scanner = DLPScanner()
    candidate = "Qm9vcmRpbmdseVRlY2huaWNhbFNlY3JldDEyMzQ1Njc4OTA"
    scanned, alerts = scanner.scan(f"value={candidate} end")
    assert candidate in scanned
    assert any("entropy" in a for a in alerts)


def test_dlp_entropy_redaction_is_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PERUNCHAT_DLP_ENTROPY_REDACT", "1")
    scanner = DLPScanner()
    candidate = "Qm9vcmRpbmdseVRlY2huaWNhbFNlY3JldDEyMzQ1Njc4OTA"
    scanned, alerts = scanner.scan(candidate)
    assert candidate not in scanned
    assert "[REDACTED_SECRET]" in scanned
    assert any("entropy" in a for a in alerts)


def test_dlp_still_redacts_labelled_secrets() -> None:
    scanner = DLPScanner()
    scanned, alerts = scanner.scan("key: prn_live_" + "a1b2c3d4" * 6)
    assert "prn_live_" not in scanned
    assert any("perun_token" in a for a in alerts)


def test_scan_egress_keeps_identifier_content_but_flags_secrets() -> None:
    pipeline = SecurityPipeline()
    frame = {
        "payload": {
            "op": "create",
            "content": "docs/HARDENING_BACKLOG_PART3.md holds items 12-20.",
            "note": "token is prn_live_" + "a1b2c3d4" * 6,
        }
    }
    safe = pipeline.scan_egress(frame)
    assert safe["payload"]["content"] == frame["payload"]["content"]
    assert "prn_live_" not in safe["payload"]["note"]
    assert safe["_dlp_redacted"] is True


def test_token_bucket_starts_full_for_its_capacity() -> None:
    bucket = TokenBucket(capacity=3, refill_per_second=0)
    assert [bucket.consume(now=0) for _ in range(3)] == [True, True, True]
    assert bucket.consume(now=0) is False
