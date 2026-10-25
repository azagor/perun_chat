"""
REST API for key provisioning, HITL approvals, and social graph management.

Endpoints from master specification §4.2:
  POST   /api/v1/keys        - create agent key
  GET    /api/v1/keys        - list keys
  DELETE /api/v1/keys/{id}   - revoke key
  PATCH  /api/v1/keys/{id}/scopes - update scopes

Plus:
  POST   /api/v1/proposals/{id}/approve - issue approval token
  POST   /api/v1/proposals/{id}/reject - reject proposal

Social graph (friends / blocked / friend requests):
  POST   /api/v1/social/friends/requests            - send friend request
  GET    /api/v1/social/friends/requests            - list pending requests
  POST   /api/v1/social/friends/requests/{id}/accept - accept friend request
  POST   /api/v1/social/friends/requests/{id}/reject - reject/cancel friend request
  GET    /api/v1/social/friends                     - list friends
  DELETE /api/v1/social/friends/{agent_id}           - remove friend
  POST   /api/v1/social/blocked                     - block an agent
  GET    /api/v1/social/blocked                     - list blocked agents
  DELETE /api/v1/social/blocked/{agent_id}           - unblock an agent
"""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import config, db as db_module
from .hitl import HITLConfig, HITLTokenEngine
from .keys import DuplicateAgentIdError, InvalidAgentIdError, KeyStore
from .models import AgentKey, Runtime
from .rooms import parse_room_id, private_room_id
from .social import SocialGraph, SocialGraphError

router = APIRouter(prefix="/api/v1")

# Shared state: in production these live in Redis/Postgres and are injected.
keystore = KeyStore(db_path=config.db_path())
social_graph = SocialGraph(db_path=config.db_path())
hitl_config = HITLConfig(secret_key=config.secret_key())
hitl_engine = HITLTokenEngine(hitl_config)

# Pending proposals keyed by proposal_id. Maps to the frame that staged them.
pending_proposals: Dict[str, Dict[str, Any]] = {}

# Proposals older than this many seconds are considered stale and may be evicted.
_PROPOSAL_TTL_SECONDS = 600


class CreateKeyRequest(BaseModel):
    owner_id: str
    agent_id: str
    supported_runtime: Runtime | str
    scopes: List[str]
    label: str | None = None


class ScopesPatch(BaseModel):
    scopes: List[str]


class ApproveRequest(BaseModel):
    execution_details: Dict[str, Any]


class RejectRequest(BaseModel):
    reason: str = "rejected_by_operator"


class FriendRequest(BaseModel):
    to_agent_id: str


class BlockRequest(BaseModel):
    blocked_agent_id: str


class CreatePrivateRoomRequest(BaseModel):
    members: List[str]
    room_id: str | None = None


class CreateGroupRoomRequest(BaseModel):
    room_id: str
    members: List[str] = []
    managed_by: str | None = None


class AddRoomMemberRequest(BaseModel):
    agent_id: str


def get_keystore() -> KeyStore:
    return keystore


def get_hitl() -> HITLTokenEngine:
    return hitl_engine


def get_social() -> SocialGraph:
    return social_graph


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if authorization.lower().startswith("bearer "):
        return authorization[7:]
    return None


def _authenticate_agent(authorization: Optional[str]) -> AgentKey:
    """Authenticate a caller by their API key and return the bound agent key."""
    token = _extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing_authorization")
    record = keystore.authenticate(token)
    if record is None:
        raise HTTPException(status_code=401, detail="invalid_or_revoked_key")
    return record


def get_current_agent(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> AgentKey:
    return _authenticate_agent(authorization)


_DASHBOARD_SECRET = config.dashboard_secret()


def require_dashboard_secret(
    x_dashboard_secret: Optional[str] = Header(default=None, alias="X-Perun-Dashboard-Secret"),
) -> None:
    """Verify the public dashboard shared secret for key-provisioning calls."""
    if not x_dashboard_secret or x_dashboard_secret != _DASHBOARD_SECRET:
        raise HTTPException(status_code=403, detail="invalid_dashboard_secret")


def _persist_room_member(room_id: str, agent_id: str) -> None:
    """Persist a room membership so ACLs survive gateway restarts."""
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        with db_module.db(config.db_path()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO room_members (room_id, agent_id, joined_at)
                VALUES (?, ?, ?)
                """,
                (room_id, agent_id.lower(), now),
            )
            conn.commit()
    except Exception:
        pass


def _remove_room_member(room_id: str, agent_id: str) -> None:
    """Remove a persisted room membership."""
    try:
        with db_module.db(config.db_path()) as conn:
            conn.execute(
                "DELETE FROM room_members WHERE room_id = ? AND agent_id = ?",
                (room_id, agent_id.lower()),
            )
            conn.commit()
    except Exception:
        pass


def _on_friendship_created(agent_a: str, agent_b: str) -> None:
    """Add both agents to each other's private room ACL.

    Import gateway lazily to avoid the circular import between api.py and
    gateway.py.
    """
    room_id = private_room_id(agent_a, agent_b)
    a = agent_a.lower()
    b = agent_b.lower()
    _persist_room_member(room_id, a)
    _persist_room_member(room_id, b)
    try:
        from .gateway import gateway

        gateway.rooms.add_member(room_id, a)
        gateway.rooms.add_member(room_id, b)
    except Exception:
        # Social graph must survive even if the gateway singleton is not ready.
        pass


def _on_friendship_removed(agent_a: str, agent_b: str) -> None:
    """Remove both agents from each other's private room ACL."""
    room_id = private_room_id(agent_a, agent_b)
    a = agent_a.lower()
    b = agent_b.lower()
    _remove_room_member(room_id, a)
    _remove_room_member(room_id, b)
    try:
        from .gateway import gateway

        members = gateway.rooms._acls.get(room_id)
        if members:
            members.discard(a)
            members.discard(b)
    except Exception:
        pass


def _add_agent_to_room_acl(room_id: str, agent_id: str) -> None:
    aid = agent_id.lower()
    _persist_room_member(room_id, aid)
    try:
        from .gateway import gateway

        gateway.rooms.add_member(room_id, aid)
    except Exception:
        pass


def _remove_agent_from_room_acl(room_id: str, agent_id: str) -> None:
    aid = agent_id.lower()
    _remove_room_member(room_id, aid)
    try:
        from .gateway import gateway

        members = getattr(gateway.rooms, "_acls", {}).get(room_id)
        if members:
            members.discard(aid)
    except Exception:
        pass


def _room_managed_by(room_id: str) -> str | None:
    """Return the manager agent id for a managed room, or None."""
    try:
        with db_module.db(config.db_path()) as conn:
            row = conn.execute(
                "SELECT managed_by FROM rooms WHERE room_id = ?", (room_id,)
            ).fetchone()
            return row["managed_by"] if row else None
    except Exception:
        return None


def _is_room_manager(caller: AgentKey, room_id: str) -> bool:
    managed_by = _room_managed_by(room_id)
    if not managed_by:
        return False
    return caller.agent_id.lower() == managed_by.lower()


def _persist_managed_room(room_id: str, managed_by: str) -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with db_module.db(config.db_path()) as conn:
        conn.execute(
            """INSERT INTO rooms (room_id, managed_by, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(room_id) DO UPDATE SET
                 managed_by=excluded.managed_by,
                 updated_at=excluded.updated_at""",
            (room_id, managed_by.lower(), now, now),
        )
        conn.commit()


def _delete_managed_room(room_id: str) -> None:
    """Remove a managed room and all its memberships from durable storage."""
    with db_module.db(config.db_path()) as conn:
        conn.execute("DELETE FROM room_members WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
        conn.commit()
    try:
        from .gateway import gateway

        gateway.rooms._acls.pop(room_id, None)
    except Exception:
        pass


def _evict_stale_proposals() -> None:
    """Remove proposals that have not been acted on within the TTL window."""
    now = int(time.time())
    stale = [
        pid
        for pid, proposal in pending_proposals.items()
        if now - proposal.get("_staged_at", 0) > _PROPOSAL_TTL_SECONDS
    ]
    for pid in stale:
        pending_proposals.pop(pid, None)


@router.post("/keys")
async def create_key(
    req: CreateKeyRequest,
    request: Request,
    store: KeyStore = Depends(get_keystore),
    x_dashboard_secret: Optional[str] = Header(default=None, alias="X-Perun-Dashboard-Secret"),
) -> Dict[str, Any]:
    """Create an agent key. Owner isolation is enforced server-side.

    The public dashboard sends a shared placeholder owner_id; for real
    self-serve registration we generate a unique owner_id here so each
    browser/session only manages the keys it created.

    Key creation is open to the public but rate-limited per IP. Requests that
    include the dashboard secret bypass the per-IP limit and use the global
    creation limiter instead.
    """
    from .gateway import gateway as _gateway
    from .security import get_client_ip

    dashboard_ok = x_dashboard_secret is not None and x_dashboard_secret == _DASHBOARD_SECRET
    if dashboard_ok:
        if not await _gateway.security.check_creation_allowed():
            raise HTTPException(status_code=429, detail="key_creation_rate_limited")
    else:
        ip = get_client_ip(request)
        if not await _gateway.security.public_creation_limiter.attempt(ip):
            raise HTTPException(status_code=429, detail="key_creation_rate_limited")
    owner_id = (req.owner_id or "").strip()
    if not owner_id or owner_id == "dashboard-user":
        owner_id = uuid.uuid4().hex
    try:
        raw, record = store.create(
            owner_id=owner_id,
            agent_id=req.agent_id,
            supported_runtime=req.supported_runtime,
            scopes=req.scopes,
            label=req.label,
        )
    except DuplicateAgentIdError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except InvalidAgentIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "plaintext_key": raw,
        "owner_id": record.owner_id,
        "key": {
            "id": record.id,
            "agent_id": record.agent_id,
            "supported_runtime": record.supported_runtime,
            "key_prefix": record.key_prefix,
            "scopes": record.scopes,
            "created_at": record.created_at.isoformat(),
        },
    }


@router.get("/keys/availability/{agent_id}")
def check_agent_id(
    agent_id: str,
    store: KeyStore = Depends(get_keystore),
) -> Dict[str, Any]:
    """Public availability check for an agent_id."""
    available = store.is_agent_id_available(agent_id)
    return {"ok": True, "agent_id": agent_id, "available": available}


@router.get("/me")
def me(agent: AgentKey = Depends(get_current_agent)) -> Dict[str, Any]:
    """Return the identity bound to the authenticated API key."""
    return {
        "ok": True,
        "agent_id": agent.agent_id,
        "supported_runtime": agent.supported_runtime,
        "scopes": agent.scopes,
        "label": agent.label,
    }


@router.get("/keys")
def list_keys(
    caller: AgentKey = Depends(get_current_agent),
    store: KeyStore = Depends(get_keystore),
) -> Dict[str, Any]:
    """List keys owned by the caller. The API key itself is the credential."""
    keys = [
        {
            "id": k.id,
            "agent_id": k.agent_id,
            "label": k.label,
            "supported_runtime": k.supported_runtime,
            "key_prefix": k.key_prefix,
            "scopes": k.scopes,
            "created_at": k.created_at.isoformat(),
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
        }
        for k in store.list_by_owner(caller.owner_id)
    ]
    return {"ok": True, "keys": keys}


@router.delete("/keys/{key_id}")
def revoke_key(
    key_id: str,
    caller: AgentKey = Depends(get_current_agent),
    store: KeyStore = Depends(get_keystore),
) -> Dict[str, Any]:
    """Revoke a key. The caller must own it (same owner_id as their API key)."""
    record = store.revoke_by_owner(key_id, caller.owner_id)
    if record is None:
        raise HTTPException(status_code=404, detail="key_not_found_or_not_owned")
    return {"ok": True, "id": record.id, "revoked_at": record.revoked_at}


@router.patch("/keys/{key_id}/scopes")
def update_key_scopes(
    key_id: str,
    req: ScopesPatch,
    store: KeyStore = Depends(get_keystore),
    _: None = Depends(require_dashboard_secret),
) -> Dict[str, Any]:
    record = store.update_scopes(key_id, req.scopes)
    if record is None:
        raise HTTPException(status_code=404, detail="key_not_found")
    return {"ok": True, "id": record.id, "scopes": record.scopes}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: str,
    req: ApproveRequest,
    engine: HITLTokenEngine = Depends(get_hitl),
) -> Dict[str, Any]:
    _evict_stale_proposals()
    proposal = pending_proposals.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal_not_found")
    # Bind the approval token to the *original* execution details staged with the
    # proposal, not whatever the dashboard sends.
    original_details = (
        proposal.get("payload", {}).get("execution_details")
        if proposal.get("content_type") == "application/perun-proposal"
        else None
    )
    if original_details is None:
        raise HTTPException(status_code=400, detail="proposal_missing_execution_details")
    token = engine.issue(proposal_id, original_details)
    return {"ok": True, "approval_token": token}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: str,
    req: RejectRequest,
) -> Dict[str, Any]:
    _evict_stale_proposals()
    proposal = pending_proposals.pop(proposal_id, None)
    if proposal is None:
        raise HTTPException(status_code=404, detail="proposal_not_found")
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "status": "rejected",
        "reason": req.reason,
    }


# ---------------------------------------------------------------------------
# Social graph endpoints
# ---------------------------------------------------------------------------


@router.post("/social/friends/requests")
def send_friend_request(
    req: FriendRequest,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        result = social_graph.request_friend(caller.agent_id, req.to_agent_id)
    except SocialGraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "request": result}


@router.get("/social/friends/requests")
def list_friend_requests(
    direction: str = "all",
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        requests = social_graph.list_requests(caller.agent_id, direction=direction)
    except SocialGraphError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "requests": requests}


@router.post("/social/friends/requests/{request_id}/accept")
def accept_friend_request(
    request_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        result = social_graph.accept_friend(request_id, caller.agent_id)
    except SocialGraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _on_friendship_created(result["from_agent_id"], result["to_agent_id"])
    return {"ok": True, "friendship": result}


@router.post("/social/friends/requests/{request_id}/reject")
def reject_friend_request(
    request_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        result = social_graph.reject_friend(request_id, caller.agent_id)
    except SocialGraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "request": result}


@router.get("/social/friends")
def list_friends(
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    return {"ok": True, "friends": social_graph.list_friends(caller.agent_id)}


@router.get("/social/friends/status")
def friends_status(
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    """Return the online/offline status of all confirmed friends."""
    try:
        from .gateway import gateway

        online = gateway.online_agent_ids()
    except Exception:
        online = set()
    friends = social_graph.list_friends(caller.agent_id)
    result = [
        {"agent_id": friend_id, "status": "online" if friend_id.lower() in online else "offline"}
        for friend_id in friends
    ]
    return {"ok": True, "friends": result}


@router.delete("/social/friends/{agent_id}")
def remove_friend(
    agent_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        result = social_graph.remove_friend(caller.agent_id, agent_id)
    except SocialGraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _on_friendship_removed(caller.agent_id, agent_id)
    return {"ok": True, "removed": result}


@router.post("/social/blocked")
def block_agent(
    req: BlockRequest,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        result = social_graph.block(caller.agent_id, req.blocked_agent_id)
    except SocialGraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _on_friendship_removed(caller.agent_id, req.blocked_agent_id)
    return {"ok": True, "blocked": result}


@router.get("/social/blocked")
def list_blocked(
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    return {"ok": True, "blocked": social_graph.list_blocked(caller.agent_id)}


@router.delete("/social/blocked/{agent_id}")
def unblock_agent(
    agent_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    try:
        result = social_graph.unblock(caller.agent_id, agent_id)
    except SocialGraphError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "unblocked": result}


@router.post("/rooms/private")
def create_private_room(
    req: CreatePrivateRoomRequest,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    import uuid

    members = set()
    members.add(caller.agent_id.lower())
    for m in req.members or []:
        if m and m.strip():
            members.add(m.strip().lower())
    if len(members) < 2:
        raise HTTPException(status_code=400, detail="need_at_least_two_members")
    if req.room_id and req.room_id.strip():
        room_id = req.room_id.strip()
        if not room_id.startswith("priv:"):
            raise HTTPException(status_code=400, detail="private_room_must_start_with_priv:")
    elif len(members) == 2:
        a, b = sorted(members)
        room_id = private_room_id(a, b)
    else:
        room_id = f"priv:m:{uuid.uuid4().hex}"
    for m in members:
        _add_agent_to_room_acl(room_id, m)
    return {"ok": True, "room_id": room_id, "members": sorted(list(members))}


@router.post("/rooms/group")
def create_group_room(
    req: CreateGroupRoomRequest,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    """Create or idempotently update a managed group room.

    Managed rooms have an exclusive owner (``managed_by``) that controls
    membership and lifecycle. They are used for project-bound chats where the
    room's lifetime is tied to an external object (e.g. a Perun project).
    """
    room_id = (req.room_id or "").strip()
    if not room_id.startswith("grp:"):
        raise HTTPException(status_code=400, detail="group_room_must_start_with_grp:")

    managed_by = (req.managed_by or caller.agent_id).strip().lower()
    if managed_by != caller.agent_id.lower():
        # A caller may only create rooms it owns. A scope-based admin key could
        # relax this later, but for now ownership is explicit.
        raise HTTPException(status_code=403, detail="cannot_create_room_for_other_owner")

    existing_manager = _room_managed_by(room_id)
    if existing_manager and existing_manager.lower() != managed_by:
        raise HTTPException(status_code=409, detail="room_already_managed_by_another_agent")

    members = set()
    members.add(managed_by)
    for m in req.members or []:
        if m and m.strip():
            members.add(m.strip().lower())

    _persist_managed_room(room_id, managed_by)
    for m in members:
        _add_agent_to_room_acl(room_id, m)

    return {
        "ok": True,
        "room_id": room_id,
        "members": sorted(list(members)),
        "managed_by": managed_by,
    }


@router.get("/rooms/private")
def list_private_rooms(
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    agent_id = caller.agent_id.lower()
    try:
        with db_module.db(config.db_path()) as conn:
            rows = conn.execute(
                "SELECT room_id FROM room_members WHERE agent_id = ?",
                (agent_id,),
            ).fetchall()
    except Exception:
        rows = []
    private_rooms = []
    for row in rows:
        room_id = row["room_id"]
        if room_id.startswith("priv:"):
            private_rooms.append(room_id)
    return {"ok": True, "rooms": sorted(set(private_rooms))}


@router.get("/rooms/{room_id}/members")
def get_room_members(
    room_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    agent_id = caller.agent_id.lower()
    try:
        from .gateway import gateway

        if _is_room_manager(caller, room_id):
            pass
        elif agent_id not in gateway.rooms._acls.get(room_id, set()):
            if not gateway.rooms.can_join(room_id, agent_id, list(caller.scopes or [])):
                raise HTTPException(status_code=403, detail="not_allowed")
    except HTTPException:
        raise
    except Exception:
        pass
    try:
        with db_module.db(config.db_path()) as conn:
            rows = conn.execute(
                "SELECT agent_id FROM room_members WHERE room_id = ?",
                (room_id,),
            ).fetchall()
        members = [r["agent_id"] for r in rows]
    except Exception:
        members = []
    return {"ok": True, "room_id": room_id, "members": sorted(members)}


@router.post("/rooms/{room_id}/members")
def add_room_member(
    room_id: str,
    req: AddRoomMemberRequest,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    caller_id = caller.agent_id.lower()
    scope, _ = parse_room_id(room_id)
    if scope == "grp":
        if not _is_room_manager(caller, room_id):
            raise HTTPException(status_code=403, detail="not_room_manager")
    elif scope == "priv":
        try:
            from .gateway import gateway

            if not gateway.rooms.can_join(room_id, caller_id, list(caller.scopes or [])):
                raise HTTPException(status_code=403, detail="not_allowed")
        except HTTPException:
            raise
        except Exception:
            pass
    else:
        raise HTTPException(status_code=400, detail="can_only_manage_private_or_group_rooms")
    _add_agent_to_room_acl(room_id, req.agent_id)
    return {"ok": True, "room_id": room_id, "agent_id": req.agent_id.lower()}


@router.delete("/rooms/{room_id}/members/{agent_id}")
def remove_room_member(
    room_id: str,
    agent_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    caller_id = caller.agent_id.lower()
    scope, _ = parse_room_id(room_id)
    if scope == "grp":
        if not _is_room_manager(caller, room_id):
            raise HTTPException(status_code=403, detail="not_room_manager")
    elif scope == "priv":
        try:
            from .gateway import gateway

            if not gateway.rooms.can_join(room_id, caller_id, list(caller.scopes or [])):
                raise HTTPException(status_code=403, detail="not_allowed")
        except HTTPException:
            raise
        except Exception:
            pass
    else:
        raise HTTPException(status_code=400, detail="can_only_manage_private_or_group_rooms")
    _remove_agent_from_room_acl(room_id, agent_id)
    return {"ok": True, "room_id": room_id, "agent_id": agent_id.lower()}


@router.delete("/rooms/{room_id}")
def delete_managed_room(
    room_id: str,
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    """Delete a managed room and all its memberships.

    Only the manager may destroy a managed room; this enforces lifecycle
    coupling with the owning system (e.g. a Perun shared project).
    """
    scope, _ = parse_room_id(room_id)
    if scope != "grp":
        raise HTTPException(status_code=400, detail="only_managed_group_rooms_may_be_deleted")
    if not _is_room_manager(caller, room_id):
        raise HTTPException(status_code=403, detail="not_room_manager")
    _delete_managed_room(room_id)
    return {"ok": True, "room_id": room_id, "deleted": True}


@router.get("/rooms")
def list_rooms(
    caller: AgentKey = Depends(get_current_agent),
) -> Dict[str, Any]:
    """List rooms the caller can discover and join.

    Combines three sources so clients (e.g. the Perun dashboard auto-join
    picker) never have to hand-type room IDs:

      * persisted memberships from ``room_members`` (friendship private rooms
        plus public rooms any agent has joined — the durable catalog),
      * rooms currently active in the gateway (live member counts),
      * well-known public rooms from ``config.public_rooms()``.

    Only rooms the caller's key is allowed to join are returned.
    """
    agent_id = caller.agent_id.lower()
    scopes = list(caller.scopes or [])

    # Lazy import: gateway.py imports this module at import time.
    from .gateway import gateway

    catalog: Dict[str, Dict[str, Any]] = {}
    type_map = {"pub": "public", "grp": "group", "priv": "private"}

    def _add(room_id: str, *, joined: bool = False, active: bool = False,
             members: int = 0, connections: int = 0) -> None:
        try:
            if not gateway.rooms.can_join(room_id, agent_id, scopes=scopes):
                return  # not discoverable with this key's scopes
            scope, _ = parse_room_id(room_id)
        except ValueError:
            return
        entry = catalog.setdefault(
            room_id,
            {
                "room_id": room_id,
                "type": type_map.get(scope, scope),
                "joined": False,
                "active": False,
                "members": 0,
                "connections": 0,
            },
        )
        entry["joined"] = entry["joined"] or joined
        entry["active"] = entry["active"] or active
        entry["members"] = max(entry["members"], members)
        entry["connections"] = max(entry["connections"], connections)

    # 1. Durable catalog: every persisted membership, aggregated per room.
    member_ids: Dict[str, set] = {}
    member_counts: Dict[str, int] = {}
    try:
        with db_module.db(config.db_path()) as conn:
            rows = conn.execute(
                "SELECT room_id, agent_id FROM room_members"
            ).fetchall()
        for row in rows:
            member_counts[row["room_id"]] = member_counts.get(row["room_id"], 0) + 1
            member_ids.setdefault(row["room_id"], set()).add(row["agent_id"])
    except Exception:
        pass
    for room_id, count in member_counts.items():
        _add(
            room_id,
            joined=agent_id in member_ids.get(room_id, set()),
            members=count,
        )

    # 2. Rooms currently active in the gateway (live connections).
    for room_id, room in list(getattr(gateway, "_rooms", {}).items()):
        _add(
            room_id,
            joined=agent_id in member_ids.get(room_id, set()),
            active=True,
            members=member_counts.get(room_id, 0),
            connections=len(room.connections),
        )

    # 3. Well-known public rooms so there is always something to pick.
    for room_id in config.public_rooms():
        _add(room_id, joined=agent_id in member_ids.get(room_id, set()),
             members=member_counts.get(room_id, 0))

    rooms = sorted(
        catalog.values(),
        key=lambda r: (not r["active"], not r["joined"], r["room_id"]),
    )
    return {"ok": True, "agent_id": agent_id, "rooms": rooms}


__all__ = [
    "router",
    "keystore",
    "social_graph",
    "hitl_engine",
    "pending_proposals",
    "get_current_agent",
    "list_rooms",
]
