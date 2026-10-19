"""
API key lifecycle management.

Plaintext API keys are returned exactly once upon creation. Persistent storage
retains only the SHA-256 hash and metadata in SQLite (default) or an in-memory
DB for tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from . import db as db_module
from .models import AgentKey, Runtime
from .security import generate_api_key


# Identities that cannot be registered to avoid confusion with system messages.
_RESERVED_AGENT_IDS: Set[str] = {
    "operator",
    "admin",
    "system",
    "root",
    "perun",
    "peruninthemachine",
    "guest",
    "anonymous",
    "server",
    "gateway",
}


class DuplicateAgentIdError(ValueError):
    """Raised when an active API key already exists for the requested agent_id."""


class InvalidAgentIdError(ValueError):
    """Raised when agent_id format is invalid or reserved."""


class KeyStore:
    """SQLite-backed key store. Pass db_path=':memory:' for tests."""

    MAX_AGENT_ID_LEN = 64
    AGENT_ID_RE = re.compile(r"^[a-zA-Z0-9._:-]+$")

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        if db_path == ":memory:":
            # Shared-cache in-memory DB so multiple connections see the same tables.
            # A UUID guarantees isolation even if Python object ids are reused.
            self._db_token = f"{uuid.uuid4().hex}"
            self._shared_uri = f"file:mem_{self._db_token}?mode=memory&cache=shared"
            self._keepalive = db_module._connect(self._shared_uri)
            self._keepalive.row_factory = sqlite3.Row
        else:
            self._db_token = None
            self._shared_uri = db_path
            self._keepalive = None
        db_module.init_db(self._shared_uri)

    def _db_uri(self) -> str:
        return self._shared_uri

    def close(self) -> None:
        if self._keepalive is not None:
            self._keepalive.close()
            self._keepalive = None

    def _validate_agent_id(self, agent_id: str) -> None:
        if not isinstance(agent_id, str) or len(agent_id) < 3:
            raise InvalidAgentIdError("agent_id must be at least 3 characters")
        if len(agent_id) > self.MAX_AGENT_ID_LEN:
            raise InvalidAgentIdError(f"agent_id must be at most {self.MAX_AGENT_ID_LEN} characters")
        if not self.AGENT_ID_RE.match(agent_id):
            raise InvalidAgentIdError("agent_id may only contain letters, digits, '.', '_', ':', '-'")
        if agent_id.lower() in _RESERVED_AGENT_IDS:
            raise InvalidAgentIdError("agent_id is reserved")

    def _active_by_agent_id(self, agent_id: str) -> Optional[AgentKey]:
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_keys WHERE LOWER(agent_id) = LOWER(?) AND is_revoked = 0",
                (agent_id,),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def is_agent_id_available(self, agent_id: str) -> bool:
        """Return True if no active key owns this agent_id."""
        try:
            self._validate_agent_id(agent_id)
        except InvalidAgentIdError:
            return False
        return self._active_by_agent_id(agent_id) is None

    def create(
        self,
        owner_id: str,
        agent_id: str,
        supported_runtime: Runtime | str,
        scopes: List[str],
        label: Optional[str] = None,
        live: bool = False,
    ) -> tuple[str, AgentKey]:
        self._validate_agent_id(agent_id)
        if self._active_by_agent_id(agent_id) is not None:
            raise DuplicateAgentIdError(f"agent_id '{agent_id}' is already registered by an active key")

        prefix = "prn_live_" if live else "prn_test_"
        raw = generate_api_key(prefix)
        key_hash = AgentKey.hash_raw_key(raw)
        key_prefix = raw[: len(prefix) + 8]

        record = AgentKey(
            owner_id=owner_id,
            agent_id=agent_id,
            label=label,
            supported_runtime=supported_runtime,
            key_prefix=key_prefix,
            key_hash=key_hash,
            scopes=scopes,
        )
        with db_module.db(self._db_uri()) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO agent_keys
                    (id, owner_id, agent_id, label, supported_runtime, key_prefix, key_hash, scopes, created_at, revoked_at, is_revoked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.owner_id,
                        record.agent_id,
                        record.label,
                        str(record.supported_runtime),
                        record.key_prefix,
                        record.key_hash,
                        json.dumps(record.scopes),
                        record.created_at.isoformat(),
                        None,
                        0,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                # The partial unique index on active agent_id is the ultimate guard.
                if "idx_agent_keys_active_agent_id" in str(exc) or "UNIQUE" in str(exc):
                    raise DuplicateAgentIdError(f"agent_id '{agent_id}' is already registered by an active key") from exc
                raise
        return raw, record

    def revoke(self, key_id: str) -> Optional[AgentKey]:
        revoked_at = datetime.now(timezone.utc)
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_keys WHERE id = ? AND is_revoked = 0", (key_id,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE agent_keys SET is_revoked = 1, revoked_at = ? WHERE id = ?",
                (revoked_at.isoformat(), key_id),
            )
            conn.commit()
            record = self._row_to_record(row)
            if record:
                record.is_revoked = True
                record.revoked_at = revoked_at
            return record

    def update_scopes(self, key_id: str, scopes: List[str]) -> Optional[AgentKey]:
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute("SELECT * FROM agent_keys WHERE id = ?", (key_id,)).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE agent_keys SET scopes = ? WHERE id = ?",
                (json.dumps(scopes), key_id),
            )
            conn.commit()
            record = self._row_to_record(row)
            if record:
                record.scopes = scopes
            return record

    def authenticate(self, raw_key: str) -> Optional[AgentKey]:
        """Return key metadata if raw token hash matches a non-revoked key."""
        key_hash = AgentKey.hash_raw_key(raw_key)
        with db_module.db(self._db_uri()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_keys WHERE key_hash = ? AND is_revoked = 0", (key_hash,)
            ).fetchone()
            return self._row_to_record(row) if row else None

    def list(self) -> List[AgentKey]:
        with db_module.db(self._db_uri()) as conn:
            rows = conn.execute("SELECT * FROM agent_keys ORDER BY created_at DESC").fetchall()
            return [self._row_to_record(row) for row in rows if row]

    def _row_to_record(self, row: Optional[sqlite3.Row]) -> Optional[AgentKey]:
        if row is None:
            return None
        data = db_module.row_to_dict(row)
        scopes = json.loads(data.get("scopes") or "[]")
        created_at = datetime.fromisoformat(data["created_at"])
        revoked_at = None
        if data.get("revoked_at"):
            revoked_at = datetime.fromisoformat(data["revoked_at"])
        return AgentKey(
            id=data["id"],
            owner_id=data["owner_id"],
            agent_id=data["agent_id"],
            label=data.get("label"),
            supported_runtime=data["supported_runtime"],
            key_prefix=data["key_prefix"],
            key_hash=data["key_hash"],
            scopes=scopes,
            created_at=created_at,
            revoked_at=revoked_at,
            is_revoked=bool(data["is_revoked"]),
        )


__all__ = ["KeyStore", "DuplicateAgentIdError", "InvalidAgentIdError"]
