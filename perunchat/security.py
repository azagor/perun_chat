"""
PerunChat security pipeline.

Implements:
  - egress DLP scanning and redaction
  - token-bucket rate limiting (async-safe)
  - monotonic sequence / nonce / timestamp validation
  - per-room regenerating turn budget and circuit breaker
  - authentication-failure rate limiting
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from fastapi import Request


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


# Identifiers that must never be treated as secrets: SCREAMING_SNAKE names,
# path-like tokens, and anything ending in a file extension. A chat message
# naming `docs/HARDENING_BACKLOG_PART3.md` is documentation, not a credential.
_IDENTIFIER_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_PATHISH_RE = re.compile(r"^(?:\.{0,2}/|\w+/)")
_EXTENSION_RE = re.compile(
    r"\.(?:md|txt|py|json|ya?ml|toml|ini|cfg|zip|sha256|log|ps1|sh|js|ts|html|css)$",
    re.I,
)


def _looks_like_identifier(candidate: str) -> bool:
    """True when a high-entropy run is almost certainly a name, not a secret."""
    if _IDENTIFIER_RE.match(candidate):
        return True
    if _PATHISH_RE.match(candidate) or _EXTENSION_RE.search(candidate):
        return True
    has_digit = any(ch.isdigit() for ch in candidate)
    has_upper = any(ch.isupper() for ch in candidate)
    # Credentials mix letters and digits in one run. Lowercase names with
    # separators (`hardening_backlog_part3`) are prose.
    if not has_digit:
        return True
    return "_" in candidate and not has_upper


class DLPScanner:
    """Scans outbound text/JSON for secrets.

    Labelled credential patterns (API keys, private keys, `NAME=value`
    assignments) are redacted. The high-entropy heuristic is **detection only**
    by default: rewriting fuzzy matches corrupts ordinary technical text —
    filenames, identifiers, base64 patches — which is worse than the leak it
    hopes to prevent, because the sender and the receiver silently disagree
    about the content. Set ``PERUNCHAT_DLP_ENTROPY_REDACT=1`` to restore
    rewriting for deployments that require it.
    """

    HIGH_ENTROPY_THRESHOLD = 4.5
    HIGH_ENTROPY_MIN_LEN = 32

    def __init__(self) -> None:
        self.entropy_redaction = os.environ.get(
            "PERUNCHAT_DLP_ENTROPY_REDACT", ""
        ).strip().lower() in {"1", "true", "yes", "on"}

    def scan(self, text: str) -> Tuple[str, List[str]]:
        """
        Return (scanned_text, list_of_alert_labels).

        Labelled matches are replaced with ``[REDACTED_SECRET]``. Entropy hits
        are reported in ``alerts`` and left intact unless explicitly enabled.
        """
        if not isinstance(text, str):
            # If payload is JSON/proposal, scan its serialized form.
            text = str(text)
        alerts: List[str] = []

        # Rebuild rather than mutating while iterating: replacing inside a
        # string that ``finditer`` is walking shifts every later match index.
        scanned = text
        for label, pattern in _DLP_PATTERNS:
            if pattern.search(scanned):
                alerts.append(f"dlp_regex:{label}")
                scanned = pattern.sub("[REDACTED_SECRET]", scanned)

        # Entropy-based detection for remaining long high-entropy strings.
        scanned, entropy_alerts = self._scan_high_entropy(scanned, [])
        alerts.extend(entropy_alerts)

        return scanned, alerts

    def _scan_high_entropy(self, text: str, alerts: List[str]) -> Tuple[str, List[str]]:
        candidate_pattern = re.compile(
            r"[A-Za-z0-9+/=_-]{%d,}" % self.HIGH_ENTROPY_MIN_LEN
        )
        result = text
        offset = 0
        for match in candidate_pattern.finditer(text):
            candidate = match.group()
            if _looks_like_identifier(candidate):
                continue
            if _shannon_entropy(candidate) < self.HIGH_ENTROPY_THRESHOLD:
                continue
            alerts.append("dlp_entropy:high_entropy_string")
            if not self.entropy_redaction:
                continue
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

    def __post_init__(self) -> None:
        # Start full: the default field value cannot see ``capacity``.
        self._tokens = float(self.capacity)

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
    # Bound the per-stream sequence table. A valid client can create many
    # streams; eviction only resets ordering state (nonces still block replay).
    MAX_STREAMS = 10_000

    def __init__(self) -> None:
        self._last_seq: OrderedDict[str, int] = OrderedDict()
        self._seen_nonces: Dict[str, int] = {}
        self._prune_ops = 0

    def _prune_nonces(self, now_ms: int) -> None:
        cutoff = now_ms - self.MAX_SKEW_MS * 2
        stale = [key for key, ts in self._seen_nonces.items() if ts < cutoff]
        for key in stale:
            self._seen_nonces.pop(key, None)

    def validate(
        self,
        agent_id: str,
        runtime: str,
        sequence_number: int,
        timestamp_ms: int,
        nonce: Optional[str] = None,
        stream_id: str = "",
    ) -> Tuple[bool, Optional[str]]:
        # Different clients (e.g. the autonomous bridge vs. the browser dashboard)
        # may share an agent_id/API key and runtime. A per-client stream id keeps
        # their sequence counters separate.
        key = f"{agent_id}:{runtime}:{stream_id}"
        now_ms = int(time.time() * 1000)
        if abs(now_ms - timestamp_ms) > self.MAX_SKEW_MS:
            return False, "timestamp_skew"

        self._prune_ops += 1
        if self._prune_ops % 100 == 0 or len(self._seen_nonces) > 10_000:
            self._prune_nonces(now_ms)
        nonce_key = f"{agent_id}:{runtime}:{nonce}" if nonce is not None else ""
        if nonce is not None and nonce_key in self._seen_nonces:
            return False, "duplicate_nonce"

        last = self._last_seq.get(key, -1)
        if sequence_number <= last:
            return False, "non_monotonic_sequence"

        # Commit state only after every check has passed. A rejected frame must
        # not burn its sequence number or nonce.
        self._last_seq[key] = sequence_number
        self._last_seq.move_to_end(key)
        if len(self._last_seq) > self.MAX_STREAMS:
            self._last_seq.popitem(last=False)
        if nonce is not None:
            self._seen_nonces[nonce_key] = now_ms

        return True, None


class TurnCounter:
    """
    Tracks autonomous turns per (room_id, agent_id) with a regenerating
    credit budget and burst protection.

    - Credits refill over time (allows long unsupervised debates)
    - Hard burst cap kills rapid ping-pong
    - Long inactivity resets the budget (fresh start after breaks)
    - Operator presence grants a grace window (more permissive)
    """

    # Regenerating budget
    MAX_CREDITS = 200
    REFILL_RATE = 1.0
    REFILL_INTERVAL = 75.0

    # Burst protection
    BURST_LIMIT = 25
    BURST_SECONDS = 60.0

    # Inactivity forgiveness
    IDLE_RESET_SECONDS = 900.0

    # Operator grace
    OPERATOR_GRACE_SECONDS = 600.0

    # Runtimes that represent a human/operator-driven client
    OPERATOR_RUNTIMES = {"operator", "custom", "dashboard", "human"}

    PRUNE_AGE_SECONDS = 3600.0
    MAX_KEYS = 10000

    def __init__(self) -> None:
        self._credits: Dict[str, float] = {}
        self._last_update: Dict[str, float] = {}
        self._last_turn: Dict[str, float] = {}
        self._locked: Set[str] = set()
        self._last_seen: Dict[str, float] = {}
        self._last_operator_seen: Dict[str, float] = {}
        self._room_last_activity: Dict[str, float] = {}
        self._recent_turns: Dict[str, Deque[float]] = {}

    @staticmethod
    def _key(room_id: str, agent_id: str) -> str:
        return f"{room_id}\u0000{agent_id}"

    def _refill(self, key: str, now: float) -> float:
        last_upd = self._last_update.get(key, now)
        elapsed = now - last_upd
        if elapsed <= 0:
            return self._credits.get(key, float(self.MAX_CREDITS))

        last_turn = self._last_turn.get(key, 0.0)
        if last_turn and (now - last_turn) > self.IDLE_RESET_SECONDS:
            self._credits[key] = float(self.MAX_CREDITS)
            self._last_update[key] = now
            self._last_turn[key] = now
            return self._credits[key]

        gained = elapsed / self.REFILL_INTERVAL * self.REFILL_RATE
        credits = min(self.MAX_CREDITS, self._credits.get(key, float(self.MAX_CREDITS)) + gained)
        self._credits[key] = credits
        self._last_update[key] = now
        return credits

    def is_locked(self, room_id: str, agent_id: str = "") -> bool:
        return self._key(room_id, agent_id) in self._locked

    def increment(self, room_id: str, sender_runtime: Optional[str] = None,
                  agent_id: str = "") -> Tuple[float, bool]:
        """
        Increment turn counter for a room. Returns (credits_remaining, is_locked).
        Operator runtime resets the budget and grants room grace.
        Autonomous turns consume credits with burst protection.
        """
        key = self._key(room_id, agent_id)
        now = time.time()
        self._last_seen[key] = now
        self._room_last_activity[room_id] = now

        is_operator = (
            sender_runtime in self.OPERATOR_RUNTIMES
            or (isinstance(sender_runtime, str) and sender_runtime.endswith("-operator"))
        )

        if is_operator:
            self._credits[key] = float(self.MAX_CREDITS)
            self._last_update[key] = now
            self._last_turn[key] = now
            self._locked.discard(key)
            self._last_operator_seen[room_id] = now
            if key in self._recent_turns:
                self._recent_turns[key].clear()
            return float(self.MAX_CREDITS), False

        last_op = self._last_operator_seen.get(room_id, 0.0)
        supervised = (now - last_op) < self.OPERATOR_GRACE_SECONDS

        credits = self._refill(key, now)

        dq = self._recent_turns.setdefault(key, deque())
        while dq and dq[0] < now - self.BURST_SECONDS:
            dq.popleft()
        dq.append(now)

        if len(dq) >= self.BURST_LIMIT and not supervised:
            self._locked.add(key)
            return credits, True

        if credits < 1.0 and not supervised:
            self._locked.add(key)
            return credits, True

        if not supervised:
            self._credits[key] = credits - 1.0
        else:
            self._credits[key] = float(self.MAX_CREDITS)

        self._last_update[key] = now
        self._last_turn[key] = now
        self._locked.discard(key)
        return self._credits[key], False

    def reset(self, room_id: str, agent_id: str = "") -> None:
        key = self._key(room_id, agent_id)
        self._credits.pop(key, None)
        self._last_update.pop(key, None)
        self._last_turn.pop(key, None)
        self._locked.discard(key)
        self._last_seen.pop(key, None)
        self._recent_turns.pop(key, None)

    def prune(self) -> int:
        """Remove stale entries. Returns number of removed keys."""
        now = time.time()
        cutoff = now - self.PRUNE_AGE_SECONDS
        stale = [k for k, t in self._last_seen.items() if t < cutoff]
        if len(self._last_seen) - len(stale) > self.MAX_KEYS:
            survivors = {k: t for k, t in self._last_seen.items() if t >= cutoff}
            sorted_keys = sorted(survivors, key=survivors.get)
            extra = len(survivors) - self.MAX_KEYS
            stale.extend(sorted_keys[:extra])
        for key in stale:
            self._credits.pop(key, None)
            self._last_update.pop(key, None)
            self._last_turn.pop(key, None)
            self._locked.discard(key)
            self._last_seen.pop(key, None)
            self._recent_turns.pop(key, None)
        self._last_operator_seen = {r: t for r, t in self._last_operator_seen.items() if t >= cutoff}
        self._room_last_activity = {r: t for r, t in self._room_last_activity.items() if t >= cutoff}
        return len(stale)


class AuthRateLimiter:
    """Separate, stricter rate limiter for authentication failures."""

    MAX_KEYS = 10_000

    def __init__(self, capacity: int = 5, window_seconds: float = 60.0) -> None:
        self.capacity = capacity
        self.window_seconds = window_seconds
        self._failures: Dict[str, List[float]] = {}
        self._lock = asyncio.Lock()
        self._ops = 0
        self._last_prune = 0.0

    def _maybe_prune_locked(self, now: float) -> None:
        self._ops += 1
        if (
            now - self._last_prune >= max(self.window_seconds, 5.0)
            or self._ops % 100 == 0
            or len(self._failures) > self.MAX_KEYS
        ):
            self._last_prune = now
            self._prune_locked(now)

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.window_seconds
        stale = [
            key for key, stamps in self._failures.items()
            if not stamps or stamps[-1] < cutoff
        ]
        for key in stale:
            self._failures.pop(key, None)
        if len(self._failures) > self.MAX_KEYS:
            ordered = sorted(
                self._failures.items(),
                key=lambda item: item[1][-1] if item[1] else 0.0,
            )
            for key, _ in ordered[: len(self._failures) - self.MAX_KEYS]:
                self._failures.pop(key, None)

    async def record_failure(self, identifier: str) -> None:
        now = time.time()
        async with self._lock:
            self._maybe_prune_locked(now)
            window = self._failures.get(identifier, [])
            cutoff = now - self.window_seconds
            recent = [t for t in window if t >= cutoff]
            recent.append(now)
            self._failures[identifier] = recent

    async def is_allowed(self, identifier: str) -> bool:
        now = time.time()
        async with self._lock:
            self._maybe_prune_locked(now)
            window = self._failures.get(identifier, [])
            cutoff = now - self.window_seconds
            recent = [t for t in window if t >= cutoff]
            if recent:
                self._failures[identifier] = recent
            else:
                self._failures.pop(identifier, None)
            return len(recent) < self.capacity

    async def attempt(self, identifier: str) -> bool:
        """Record an attempt and return whether it is within the burst cap.

        Check and append happen in one critical section so concurrent attempts
        cannot both observe the last free slot.
        """
        now = time.time()
        async with self._lock:
            self._maybe_prune_locked(now)
            cutoff = now - self.window_seconds
            recent = [t for t in self._failures.get(identifier, []) if t >= cutoff]
            allowed = len(recent) < self.capacity
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
        # Stricter per-IP limit for public self-serve registration when no
        # dashboard secret is supplied. Tunable via environment for different
        # hosting profiles (defaults: 3 keys per IP per hour).
        self.public_creation_capacity = int(
            os.environ.get("PERUNCHAT_PUBLIC_CREATION_BURST", "3")
        )
        self.public_creation_window_seconds = float(
            os.environ.get("PERUNCHAT_PUBLIC_CREATION_WINDOW_SECONDS", "3600")
        )
        self.public_creation_limiter = AuthRateLimiter(
            capacity=self.public_creation_capacity,
            window_seconds=self.public_creation_window_seconds,
        )
        # Dedicated token bucket for chunked package transfers in managed project
        # rooms. These frames are exempt from the turn counter, so they get their
        # own limiter to prevent a runaway agent from flooding the connection.
        package_capacity = float(os.environ.get("PERUNCHAT_PACKAGE_RATE_CAPACITY", "60"))
        package_refill = float(os.environ.get("PERUNCHAT_PACKAGE_RATE_REFILL", "40"))
        self.package_rate_limiter = RateLimiter(
            capacity=package_capacity,
            refill_per_second=package_refill,
        )

    async def pre_validate(
        self,
        agent_id: str,
        room_id: str,
        sequence_number: int,
        timestamp_ms: int,
        thread_id: str,
        runtime: str,
        nonce: Optional[str] = None,
        content_type: str = "",
        stream_id: str = "",
    ) -> Tuple[bool, Optional[str]]:
        """Rate, sequence and timestamp checks before proposal/broadcast decisions.

        This does **not** consume a turn credit; call :meth:`commit_turn` for
        frames that will actually be broadcast.
        """
        allowed = await self.rate_limiter.allow(agent_id, room_id)
        if not allowed:
            return False, "rate_limited"

        if content_type == "application/perun-package":
            allowed = await self.package_rate_limiter.allow(agent_id, room_id)
            if not allowed:
                return False, "rate_limited"

        ok, reason = self.sequence.validate(
            agent_id,
            runtime,
            sequence_number,
            timestamp_ms,
            nonce,
            stream_id=stream_id,
        )
        if not ok:
            return False, reason

        return True, None

    def commit_turn(
        self,
        agent_id: str,
        room_id: str,
        runtime: str,
        content_type: str = "",
    ) -> Tuple[bool, Optional[str]]:
        """Consume one turn credit for a frame that is about to be broadcast.

        Returns ``(True, None)`` on success, or ``(False, reason)`` if the
        room is locked or out of credits.
        """
        if content_type == "application/perun-package":
            return True, None

        count, locked = self.turns.increment(room_id, runtime, agent_id)
        if locked:
            return False, "thread_locked_pending_operator"
        return True, None

    async def check_ingress(
        self,
        agent_id: str,
        room_id: str,
        sequence_number: int,
        timestamp_ms: int,
        thread_id: str,
        runtime: str,
        nonce: Optional[str] = None,
        content_type: str = "",
        stream_id: str = "",
    ) -> Tuple[bool, Optional[str]]:
        ok, reason = await self.pre_validate(
            agent_id=agent_id,
            room_id=room_id,
            sequence_number=sequence_number,
            timestamp_ms=timestamp_ms,
            thread_id=thread_id,
            runtime=runtime,
            nonce=nonce,
            content_type=content_type,
            stream_id=stream_id,
        )
        if not ok:
            return False, reason

        return self.commit_turn(
            agent_id=agent_id,
            room_id=room_id,
            runtime=runtime,
            content_type=content_type,
        )

    def scan_egress(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of the frame with labelled credential redaction applied.

        Only *labelled* secret patterns (API keys, private keys, ``NAME=value``
        assignments) are rewritten. The fuzzy high-entropy heuristic runs in
        detect-only mode, because rewriting it previously corrupted ordinary
        technical text — filenames, identifiers, base64 patches — so the
        proposal the executor received no longer matched the approved body.
        """
        import copy

        safe = copy.deepcopy(frame)
        payload = safe.get("payload")
        alerts: List[str] = []
        changed = False
        if isinstance(payload, str):
            scanned, alerts = self.dlp.scan(payload)
            if scanned != payload:
                safe["payload"] = scanned
                changed = True
        elif isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, str):
                    scanned, found = self.dlp.scan(value)
                    alerts.extend(found)
                    if scanned != value:
                        payload[key] = scanned
                        changed = True
        if alerts:
            existing = safe.get("_security_alerts") or []
            safe["_security_alerts"] = list(dict.fromkeys([*existing, *alerts]))
        if changed:
            safe["_dlp_redacted"] = True
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


def get_client_ip(request: Request) -> str:
    """Return the client IP, honoring X-Forwarded-For only behind a trusted proxy."""
    direct = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and is_trusted_ip(direct):
        ip = _ip_from_forwarded(forwarded)
        if ip:
            return ip
    return direct


def is_trusted_ip(ip_str: str) -> bool:
    """Return True for private/loopback addresses that are likely reverse proxies."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


def _ip_from_forwarded(forwarded: str) -> Optional[str]:
    """Extract the first IP from an X-Forwarded-For header."""
    if not forwarded:
        return None
    for part in forwarded.split(","):
        candidate = part.strip()
        if candidate:
            return candidate
    return None


__all__ = [
    "DLPScanner",
    "RateLimiter",
    "SequenceValidator",
    "TurnCounter",
    "SecurityPipeline",
    "AuthRateLimiter",
    "generate_api_key",
]

