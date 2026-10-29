"""
Human-in-the-Loop (HITL) approval token system.

Generates single-use HMAC-SHA256 approval tokens for PERUN_ACTION_PROPOSALs.
Token format from master spec:
  HMAC-SHA256(K_server, proposal_id || action_hash || 0 || nonce)

  - No expiry: proposals may wait for approvers who are offline.
  - Nonces are single-use and persisted in SQLite so replay protection survives
    gateway restarts. Time-based eviction is intentionally absent.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


@dataclass
class HITLConfig:
    secret_key: bytes
    # Kept for backward compatibility with existing call sites; the value is no
    # longer used to reject tokens. A token's expiry_epoch field is always 0.
    token_ttl_seconds: int = 0
    # Kept only for backward compatibility. Nonces are never evicted by time.
    nonce_ttl_seconds: int = 0
    db_path: str = ":memory:"
    # Fail-closed cap for the append-only consumed-nonce table. When reached,
    # new approvals are rejected with ``store_full``; no nonce is evicted.
    max_consumed_nonces: int = 100_000


class ApprovalStoreBusy(Exception):
    """Raised when the nonce store cannot be written because SQLite is busy."""


class ApprovalStoreFull(Exception):
    """Raised when the append-only nonce store has reached its safety cap."""


class ApprovalStore:
    """SQLite-backed store for consumed HITL nonces.

    Nonces are stored indefinitely. Time-based eviction is deliberately absent
    because tokens have no expiry and an old token replay must remain blocked
    across gateway restarts.
    """

    _CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS consumed_nonces (
        nonce TEXT PRIMARY KEY,
        used_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_consumed_nonces_used_at
    ON consumed_nonces (used_at);
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        window_seconds: int = 0,
        max_nonces: int = 100_000,
    ) -> None:
        # window_seconds is ignored; kept for compatibility with older tests.
        self._db_path = db_path
        self._max_nonces = max(1, int(max_nonces))
        # :memory: databases are fresh per connection, so keep one long-lived
        # connection for in-memory stores. File-backed stores open a connection
        # per operation so they remain safe across threads.
        self._memory_conn: sqlite3.Connection | None = None
        if db_path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self._db_path, timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _ensure_table(self) -> None:
        conn = self._connect()
        conn.executescript(self._CREATE_SQL)
        conn.commit()
        if self._memory_conn is None:
            conn.close()

    def _is_used(self, nonce: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM consumed_nonces WHERE nonce = ?", (nonce,)
            ).fetchone()
            return row is not None
        finally:
            if self._memory_conn is None:
                conn.close()

    def consume(self, nonce: str, now: Optional[float] = None) -> bool:
        if not isinstance(nonce, str):
            return False
        if now is None:
            now = time.time()
        conn = self._connect()
        try:
            # Fail closed before growing the append-only table past its cap.
            # Evicting a nonce would re-open a replay window for its token.
            count = conn.execute(
                "SELECT COUNT(*) FROM consumed_nonces"
            ).fetchone()[0]
            if count >= self._max_nonces:
                raise ApprovalStoreFull(
                    f"consumed nonce store is full ({count}/{self._max_nonces})"
                )
            conn.execute(
                "INSERT INTO consumed_nonces (nonce, used_at) VALUES (?, ?)",
                (nonce, int(now)),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        except sqlite3.OperationalError as exc:
            # A transient "database is locked" must not crash the websocket
            # handler. Surface it as a distinct reason so the caller can retry
            # with the same token (the nonce was not consumed).
            raise ApprovalStoreBusy(str(exc)) from exc
        finally:
            if self._memory_conn is None:
                conn.close()

    def count(self) -> int:
        """Return the number of consumed nonces currently stored."""
        conn = self._connect()
        try:
            return int(
                conn.execute("SELECT COUNT(*) FROM consumed_nonces").fetchone()[0]
            )
        finally:
            if self._memory_conn is None:
                conn.close()

    def prune_before(self, epoch: int) -> int:
        """Delete nonces consumed before *epoch* and return the row count.

        **Safety:** an unexpired token whose nonce is deleted becomes replayable.
        Only call this after rotating the HITL signing key (``PERUNCHAT_SECRET``),
        which invalidates every outstanding token.
        """
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM consumed_nonces WHERE used_at < ?", (int(epoch),)
            )
            conn.commit()
            return int(cursor.rowcount)
        finally:
            if self._memory_conn is None:
                conn.close()

    def clear(self) -> None:
        """Test helper: remove all stored nonces."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM consumed_nonces")
            conn.commit()
        finally:
            if self._memory_conn is None:
                conn.close()

    def close(self) -> None:
        """Close any database connection held by this store."""
        if self._memory_conn is not None:
            self._memory_conn.close()
            self._memory_conn = None


class HITLTokenEngine:
    def __init__(self, config: HITLConfig) -> None:
        self.config = config
        self.store = ApprovalStore(
            db_path=config.db_path,
            max_nonces=config.max_consumed_nonces,
        )

    def close(self) -> None:
        """Close the underlying nonce store."""
        self.store.close()

    @staticmethod
    def action_hash(action: Dict[str, Any]) -> str:
        """Deterministic hash of execution details for token binding."""
        import json

        if not isinstance(action, dict):
            canonical = str(action)
        else:
            canonical = json.dumps(action, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def issue(
        self,
        proposal_id: str,
        execution_details: Dict[str, Any],
    ) -> Dict[str, Any]:
        # Tokens never expire. expiry_epoch is 0 for wire compatibility.
        expiry = 0
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
        import math

        # Type hardening: malformed inputs must not reach hmac.compare_digest.
        if (
            not isinstance(expiry_epoch, (int, float))
            or isinstance(expiry_epoch, bool)
            or not math.isfinite(expiry_epoch)
        ):
            return False, "invalid_signature"

        if not isinstance(nonce, str):
            return False, "invalid_signature"

        if not isinstance(token, str):
            return False, "invalid_signature"

        ahash = self.action_hash(execution_details)
        message = f"{proposal_id}||{ahash}||{expiry_epoch}||{nonce}".encode()
        expected = hmac.new(self.config.secret_key, message, hashlib.sha256).hexdigest()
        try:
            ok = hmac.compare_digest(expected.encode(), token.encode("ascii"))
        except UnicodeEncodeError:
            return False, "invalid_signature"
        if not ok:
            return False, "invalid_signature"

        # Signature must be verified before the nonce is consumed, otherwise a
        # forged token can burn a legitimate nonce and deny the real approval.
        try:
            consumed = self.store.consume(nonce)
        except ApprovalStoreBusy:
            return False, "store_busy"
        except ApprovalStoreFull:
            return False, "store_full"
        if not consumed:
            return False, "nonce_replay"

        return True, None


__all__ = [
    "HITLConfig",
    "HITLTokenEngine",
    "ApprovalStore",
    "ApprovalStoreBusy",
    "ApprovalStoreFull",
]
