"""
Pydantic models for PerunChat wire frames, proposals, rooms, and keys.

All chat frames validate against chat-frame.v2.json shape from the master spec.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class RoomType(str, Enum):
    PRIVATE = "PRIVATE"
    GROUP = "GROUP"
    PUBLIC = "PUBLIC"


class ContentType(str, Enum):
    TEXT = "text/plain"
    JSON = "application/json"
    PROPOSAL = "application/perun-proposal"


class Runtime(str, Enum):
    PERUN = "perun"
    OPENCLAW = "openclaw"
    HERMES = "hermes"
    ZEROCLAW = "zeroclaw"
    CUSTOM = "custom"


class SenderInfo(BaseModel):
    agent_id: str
    runtime: Runtime | str
    version: str = "0.0.0"
    pubkey_fingerprint: Optional[str] = None


class ChatFrame(BaseModel):
    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    trace_id: str = Field(default_factory=lambda: f"trc_{uuid.uuid4().hex[:20]}")
    thread_id: str = "default"
    sequence_number: int = Field(ge=0)
    timestamp: int = Field(default_factory=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    room_type: RoomType | str
    room_id: str
    sender: SenderInfo
    recipient_id: Optional[str] = None
    in_reply_to: Optional[str] = None
    content_type: ContentType | str
    turn_count: int = Field(default=0, ge=0)
    payload: Any
    signature: Optional[str] = None

    @field_validator("room_id")
    @classmethod
    def validate_room_id(cls, value: str) -> str:
        if not re.match(r"^(priv|grp|pub):[a-zA-Z0-9._:-]+$", value):
            raise ValueError("room_id must follow PerunChat room taxonomy")
        return value


class Provenance(BaseModel):
    instigator: str
    context_message_id: Optional[str] = None
    justification: str


class ExecutionDetails(BaseModel):
    command: Optional[str] = None
    file_path: Optional[str] = None
    diff: Optional[str] = None
    working_dir: Optional[str] = None


class ActionProposal(BaseModel):
    proposal_id: str = Field(default_factory=lambda: f"prop_{uuid.uuid4().hex[:12]}")
    proposal_type: Literal["PERUN_ACTION_PROPOSAL"] = "PERUN_ACTION_PROPOSAL"
    risk_tier: str
    action_domain: str
    description: str
    blast_radius: Optional[str] = None
    execution_details: ExecutionDetails = Field(default_factory=ExecutionDetails)
    provenance: Provenance

    @field_validator("risk_tier")
    @classmethod
    def validate_risk_tier(cls, value: str) -> str:
        allowed = {
            "TIER_0_READ_ONLY",
            "TIER_1_SCRATCHPAD",
            "TIER_2_LOW_IMPACT",
            "TIER_3_HIGH_IMPACT",
            "TIER_4_PROHIBITED",
        }
        if value not in allowed:
            raise ValueError(f"risk_tier must be one of {allowed}")
        return value


class ProposalFrame(ChatFrame):
    content_type: Literal["application/perun-proposal"] = "application/perun-proposal"
    payload: ActionProposal


class RoomACL(BaseModel):
    room_id: str
    room_type: RoomType
    tenant_id: str
    allowed_agent_ids: list[str] = Field(default_factory=list)
    owner_id: Optional[str] = None


class AgentKey(BaseModel):
    """Stored metadata for an API key. Plaintext is never persisted."""

    id: str = Field(default_factory=lambda: f"key_{uuid.uuid4().hex[:12]}")
    owner_id: str
    agent_id: str
    label: Optional[str] = None
    supported_runtime: Runtime | str
    key_prefix: str
    key_hash: str
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    revoked_at: Optional[datetime] = None
    is_revoked: bool = False

    @staticmethod
    def hash_raw_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()


class ApprovalToken(BaseModel):
    token: str
    proposal_id: str
    action_hash: str
    expiry_epoch: int
    nonce: str
    used_at: Optional[datetime] = None
