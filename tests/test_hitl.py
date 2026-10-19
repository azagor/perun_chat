"""Tests for HITL approval token engine."""

from __future__ import annotations

import time

from perunchat.hitl import ApprovalStore, HITLConfig, HITLTokenEngine


def test_issue_and_validate_token() -> None:
    engine = HITLTokenEngine(HITLConfig(secret_key=b"test-secret"))
    details = {"command": "echo hello", "file_path": None}
    issued = engine.issue("prop_123", details)

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


def test_reject_expired_token() -> None:
    engine = HITLTokenEngine(
        HITLConfig(secret_key=b"test-secret", token_ttl_seconds=-1)
    )
    issued = engine.issue("prop_123", {})
    ok, reason = engine.validate(
        token=issued["token"],
        proposal_id=issued["proposal_id"],
        execution_details={},
        expiry_epoch=issued["expiry_epoch"],
        nonce=issued["nonce"],
    )
    assert not ok
    assert reason == "expired"


def test_approval_store_eviction() -> None:
    store = ApprovalStore(window_seconds=300)
    store.consume("nonce-1", now=0)
    assert not store.consume("nonce-1", now=299)
    assert store.consume("nonce-1", now=300)  # exactly at window boundary
