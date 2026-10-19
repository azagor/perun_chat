"""
SQLite persistence for PerunChat.

Keys, rooms, and nonces must survive gateway restarts. WAL mode is enabled so
readers do not block writers.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Generator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_keys (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    label TEXT,
    supported_runtime TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    is_revoked INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_keys_active_agent_id
ON agent_keys (LOWER(agent_id))
WHERE is_revoked = 0;

CREATE INDEX IF NOT EXISTS idx_agent_keys_owner ON agent_keys (owner_id);
CREATE INDEX IF NOT EXISTS idx_agent_keys_hash ON agent_keys (key_hash);

CREATE TABLE IF NOT EXISTS room_members (
    room_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (room_id, agent_id)
);

CREATE TABLE IF NOT EXISTS pending_proposals (
    proposal_id TEXT PRIMARY KEY,
    frame_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_proposals_created
ON pending_proposals (created_at);

-- Social graph: friend requests
CREATE TABLE IF NOT EXISTS friend_requests (
    request_id TEXT PRIMARY KEY,
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Only one pending request between any ordered pair of agents.
CREATE UNIQUE INDEX IF NOT EXISTS idx_friend_requests_pending_pair
ON friend_requests (from_agent_id, to_agent_id)
WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_friend_requests_from ON friend_requests (from_agent_id);
CREATE INDEX IF NOT EXISTS idx_friend_requests_to ON friend_requests (to_agent_id);
CREATE INDEX IF NOT EXISTS idx_friend_requests_status ON friend_requests (status);

-- Social graph: established friendships (normalized ordered pair)
CREATE TABLE IF NOT EXISTS friendships (
    agent_a TEXT NOT NULL,
    agent_b TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (agent_a, agent_b)
);

CREATE INDEX IF NOT EXISTS idx_friendships_a ON friendships (agent_a);
CREATE INDEX IF NOT EXISTS idx_friendships_b ON friendships (agent_b);

-- Social graph: per-agent block list
CREATE TABLE IF NOT EXISTS blocked_agents (
    blocker_agent_id TEXT NOT NULL,
    blocked_agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (blocker_agent_id, blocked_agent_id)
);

CREATE INDEX IF NOT EXISTS idx_blocked_blocker ON blocked_agents (blocker_agent_id);
CREATE INDEX IF NOT EXISTS idx_blocked_blocked ON blocked_agents (blocked_agent_id);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    if db_path.startswith("file:"):
        return sqlite3.connect(db_path, uri=True, check_same_thread=False)
    return sqlite3.connect(db_path, check_same_thread=False)


@contextmanager
def db(db_path: str = ":memory:") -> Generator[sqlite3.Connection, None, None]:
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if db_path != ":memory:" and not db_path.startswith("file:"):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
    finally:
        conn.close()


def init_db(db_path: str = ":memory:") -> None:
    with db(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}
