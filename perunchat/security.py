"""
PerunChat security pipeline.

Implements:
  - egress DLP scanning and redaction
  - token-bucket rate limiting (async-safe)
  - monotonic sequence / nonce / timestamp validation
  - per-thread turn counter and circuit breaker
  - authentication-failure rate limiting
"""

from __future__ import annotations

import asyncio
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# Patterns loosely matching common secret shapes.
_DLP_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (
        "ed25519_private_key",
        re.compile(r"-----BEGIN\s+OPENSSH\s+PRIVATE\s+KEY-----[\s\S]*?-----END\s+OPENSSH\s+PRIVATE\s+KEY-----"),
    ),
    (
        "rsa_private_key",
        re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    ),
    (
        "aws_access_key",
        re.compile(r"AKIA[0-9A-Z]{16}"),
    ),
    (
        "gcp_service_key",
        re.compile(r'"type":\s*"service_account"'),
    ),
    (
        "perun_token",
        re.compile(r"prn_live_[a-fA-F0-9]+"),
    ),
    (
        "env_assignment",
        re.compile(r"(?i)(password|secret|token|key)\s*=\s*[^\s\"']{8,}"),
    ),
]


def _shannon_entropy(data: str) -> float:
    """Return Shannon entropy bits per byte."""
    if not data:
        return 0.0
    length = len(data)
    freq = {ch: data.count(ch) / length for ch in set(data)}
    return -sum(p * math.log2(p) for p in freq.values())


class DLPScanner:
    """Scans outbound text/JSON for high-entropy secrets and redacts matches."""

    HIGH_ENTROPY_THRESHOLD = 4.2
    HIGH_ENTROPY_MIN_LEN = 24

    def scan(self, text: str) -> Tuple[str, List[str]]:
        """
        Return (redacted_text, list_of_alert_labels).
        Matches are replaced with `[REDACTED_SECRET]`.
        """
        if not isinstance(text, str):
            # If payload is JSON/proposal, scan its serialized form.
            text = str(text)
        alerts: List[str] = []

        # Regex-based redaction first so labelled alerts are preserved even when
        # the match also happens to be high-entropy.
        redacted = text
        for label, pattern in _DLP_PATTERNS:
            for match in pattern.finditer(redacted):
                alerts.append(f"dlp_regex:{label}")
                redacted = redacted[: match.start()] + "[REDACTED_SECRET]" + redacted[match.end() :]

        # Entropy-based redaction for remaining long high-entropy strings.
        redacted, entropy_alerts = self._redact_high_entropy(redacted, [])
        alerts.extend(entropy_alerts)

        return redacted, alerts

    def _redact_high_entropy(self, text: str, alerts: List[str]) -> Tuple[str, List[str]]:
        candidate_pattern = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
        result = text
        offset = 0
        for match in candidate_pattern.finditer(text):
            candidate = match.group()
            if _shannon_entropy(candidate) >= self.HIGH_ENTROPY_THRESHOLD:
                alerts.append("dlp_entropy:high_entropy_string")
                start = match.start() + offset
                end = match.end() + offset
                replacement = "[REDACTED_SECRET]"
                result = result[:start] + replacement + result[end:]
                offset += len(replacement) - len(candidate)
        return result, alerts


@dataclass
class TokenBucket:
    """In-memory token bucket per agent/thread key."""

    capacity: float = 10.0
    refill_per_second: float = 10.0

    _tokens: float = field(default=10.0, init=False)
    _last_update: float = field(default_factory=time.time, init=False)

    def consume(self, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._last_update = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class RateLimiter:
    """Rate limiter keyed by agent_id (+ optional room/thread for stricter limits)."""

    def __init__(
        self,
        capacity: float = 10.0,
        refill_per_second: float = 10.0,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = asyncio.Lock()

    def key(self, agent_id: str, room_id: Optional[str] = None) -> str:
        return f"{agent_id}:{room_id or '*'}"

    async def allow(self, agent_id: str, room_id: Optional[str] = None) -> bool:
        async with self._lock:
            key = self.key(agent_id, room_id)
            if key not in self._buckets:
                self._buckets[key] = TokenBucket(self.capacity, self.refill_per_second)
            return self._buckets[key].consume()


class SequenceValidator:
    """
    Enforces monotonic sequence numbers and 120-second timestamp skew.
    Also tracks seen nonces to reject replays.
    """

    MAX_SKEW_MS = 120_000

    def __init__(self) -> None:
        self._last_seq: Dict[str, int] = {}
        self._seen_nonces: Set[str] = set()

    def validate(
        self,
        agent_id: str,
        sequence_number: int,
        timestamp_ms: int,
        nonce: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        key = agent_id
        last = self._last_seq.get(key, -1)
        if sequence_number <= last:
            return False, "non_monotonic_sequence"
        self._last_seq[key] = sequence_number

        now_ms = int(time.time() * 1000)
        if abs(now_ms - timestamp_ms) > self.MAX_SKEW_MS:
            return False, "timestamp_skew"

        if nonce is not None:
            if nonce in self._seen_nonces:
                return False, "duplicate_nonce"
            self._seen_nonces.add(nonce)

        return True, None


class TurnCounter:
    """
    Tracks autonomous turns per thread. Circuit breaks at 8 consecutive turns
    without operator input, setting state LOCKED_PENDING_OPERATOR.
    """

    LIMIT = 8

    def __init__(self) -> None:
        self._counts: Dict[str, int] = {}
        self._locked: Set[str] = set()

    def is_locked(self, thread_id: str) -> bool:
        return thread_id in self._locked

    def increment(self, thread_id: str, sender_runtime: Optional[str] = None) -> Tuple[int, bool]:
        """
        Increment turn counter for a thread. Returns (new_count, is_locked).
        Operator runtime resets the counter; autonomous runtimes increment it.
        """
        if sender_runtime == "operator":
            self._counts[thread_id] = 0
            self._locked.discard(thread_id)
            return 0, False

        self._counts[thread_id] = self._counts.get(thread_id, 0) + 1
        count = self._counts[thread_id]
        if count >= self.LIMIT:
            self._locked.add(thread_id)
            return count, True
        return count, False

    def reset(self, thread_id: str) -> None:
        self._counts.pop(thread_id, None)
        self._locked.discard(thread_id)


class AuthRateLimiter:
    """Separate, stricter rate limiter for authentication failures."""

    def __init__(self, capacity: int = 5, window_seconds: float = 60.0) -> None:
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._failures: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()

    async def record_failure(self, identifier: str) -> None:
        now = time.time()
        async with self._lock:
            window = self._failures.setdefault(identifier, [])
            cutoff = now - self.window_seconds
            # Keep only failures inside the window
            self._failures[identifier] = [t for t in window if t >= cutoff]
            self._failures[identifier].append(now)

    async def is_allowed(self, identifier: str) -> bool:
        now = time.time()
        async with self._lock:
            window = self._failures.get(identifier, [])
            cutoff = now - self.window_seconds
            recent = [t for t in window if t >= cutoff]
            self._failures[identifier] = recent
            return len(recent) < self.capacity

    async def attempt(self, identifier: str) -> bool:
        """Record an attempt and return whether it is within the burst cap."""
        allowed = await self.is_allowed(identifier)
        now = time.time()
        async with self._lock:
            window = self._failures.get(identifier, [])
            cutoff = now - self.window_seconds
            recent = [t for t in window if t >= cutoff]
            recent.append(now)
            self._failures[identifier] = recent
        return allowed


class SecurityPipeline:
    """Bundles DLP, rate limiting, sequence validation, and turn counting."""

    # Global cap on self-serve key creation to prevent agent_id exhaustion.
    CREATION_BURST = 20
    CREATION_WINDOW = 60.0

    def __init__(self) -> None:
        self.dlp = DLPScanner()
        self.rate_limiter = RateLimiter()
        self.sequence = SequenceValidator()
        self.turns = TurnCounter()
        self.auth_limiter = AuthRateLimiter()
        self.creation_limiter = AuthRateLimiter(
            capacity=self.CREATION_BURST,
            window_seconds=self.CREATION_WINDOW,
        )

    async def check_ingress(
        self,
        agent_id: str,
        room_id: str,
        sequence_number: int,
        timestamp_ms: int,
        thread_id: str,
        runtime: str,
        nonce: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        allowed = await self.rate_limiter.allow(agent_id, room_id)
        if not allowed:
            return False, "rate_limited"

        ok, reason = self.sequence.validate(agent_id, sequence_number, timestamp_ms, nonce)
        if not ok:
            return False, reason

        count, locked = self.turns.increment(thread_id, runtime)
        if locked:
            return False, "thread_locked_pending_operator"

        return True, None

    def scan_egress(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of the frame with DLP redactions applied to payload text."""
        import copy

        safe = copy.deepcopy(frame)
        payload = safe.get("payload")
        if isinstance(payload, str):
            redacted, alerts = self.dlp.scan(payload)
            safe["payload"] = redacted
            if alerts:
                safe.setdefault("_security_alerts", []).extend(alerts)
        elif isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, str):
                    redacted, alerts = self.dlp.scan(value)
                    payload[key] = redacted
                    if alerts:
                        safe.setdefault("_security_alerts", []).extend(alerts)
        return safe

    async def check_auth_allowed(self, identifier: str) -> bool:
        return await self.auth_limiter.is_allowed(identifier)

    async def record_auth_failure(self, identifier: str) -> None:
        await self.auth_limiter.record_failure(identifier)

    async def check_creation_allowed(self, identifier: str = "global") -> bool:
        return await self.creation_limiter.attempt(identifier)


def generate_api_key(prefix: str = "prn_live_") -> str:
    """Generate a 256-bit CSPRNG token with the requested prefix."""
    if prefix != "prn_live_":
        raise ValueError("API key prefix must be prn_live_")
    entropy = secrets.token_hex(32)
    return f"{prefix}{entropy}"


__all__ = [
    "DLPScanner",
    "RateLimiter",
    "SequenceValidator",
    "TurnCounter",
    "SecurityPipeline",
    "AuthRateLimiter",
    "generate_api_key",
]
