"""
Human-in-the-Loop (HITL) approval token system.

Generates single-use HMAC-SHA256 approval tokens for PERUN_ACTION_PROPOSALs.
Token format from master spec:
  HMAC-SHA256(K_server, proposal_id || action_hash || expiry_epoch || nonce)

  - TTL: 180 seconds
  - Nonce stored with 300-second eviction TTL (in-memory here; Redis in prod)
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class HITLConfig:
    secret_key: bytes
    token_ttl_seconds: int = 180
    nonce_ttl_seconds: int = 300


class ApprovalStore:
    """Tracks consumed nonces to prevent token replay."""

    def __init__(self, window_seconds: int = 300) -> None:
        self._used_nonces: Dict[str, float] = {}
        self._window_seconds = window_seconds

    def consume(self, nonce: str, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.time()
        self._evict_expired(now)
        if nonce in self._used_nonces:
            return False
        self._used_nonces[nonce] = now
        return True

    def _evict_expired(self, now: float) -> None:
        # Eviction window matching spec.
        expired = [n for n, t in list(self._used_nonces.items()) if now - t >= self._window_seconds]
        for n in expired:
            self._used_nonces.pop(n, None)


class HITLTokenEngine:
    def __init__(self, config: HITLConfig) -> None:
        self.config = config
        self.store = ApprovalStore(window_seconds=config.nonce_ttl_seconds)

    @staticmethod
    def action_hash(action: Dict[str, Any]) -> str:
        """Deterministic hash of execution details for token binding."""
        canonical = str(sorted(action.items())) if isinstance(action, dict) else str(action)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def issue(
        self,
        proposal_id: str,
        execution_details: Dict[str, Any],
    ) -> Dict[str, Any]:
        expiry = int(time.time()) + self.config.token_ttl_seconds
        nonce = secrets.token_hex(16)
        ahash = self.action_hash(execution_details)
        message = f"{proposal_id}||{ahash}||{expiry}||{nonce}".encode()
        token = hmac.new(self.config.secret_key, message, hashlib.sha256).hexdigest()
        return {
            "token": token,
            "proposal_id": proposal_id,
            "action_hash": ahash,
            "expiry_epoch": expiry,
            "nonce": nonce,
        }

    def validate(
        self,
        token: str,
        proposal_id: str,
        execution_details: Dict[str, Any],
        expiry_epoch: int,
        nonce: str,
    ) -> Tuple[bool, Optional[str]]:
        import time

        if int(time.time()) > expiry_epoch:
            return False, "expired"

        if not self.store.consume(nonce):
            return False, "nonce_replay"

        ahash = self.action_hash(execution_details)
        message = f"{proposal_id}||{ahash}||{expiry_epoch}||{nonce}".encode()
        expected = hmac.new(self.config.secret_key, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, token):
            return False, "invalid_signature"

        return True, None


__all__ = ["HITLConfig", "HITLTokenEngine", "ApprovalStore"]
