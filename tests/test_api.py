"""Tests for the PerunChat REST API."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PERUNCHAT_ENV", "test")

from perunchat import api as api_module
from perunchat.gateway import app, gateway
from perunchat.keys import KeyStore
from perunchat.security import SecurityPipeline
from perunchat.social import SocialGraph


@pytest.fixture(autouse=True)
def reset_gateway(tmp_path):
    if gateway.keystore is not None:
        gateway.keystore.close()
    if api_module.social_graph is not None:
        api_module.social_graph.close()
    db_path = str(tmp_path / "perunchat.db")
    fresh = KeyStore(db_path)
    gateway.keystore = fresh
    api_module.keystore = fresh
    api_module.social_graph = SocialGraph(db_path)
    gateway.social_graph = api_module.social_graph
    gateway.security = SecurityPipeline()
    gateway.rooms._acls.clear()
    gateway._rooms.clear()
    api_module.pending_proposals.clear()


client = TestClient(app)

_DASHBOARD_SECRET = os.environ.get("PERUNCHAT_DASHBOARD_SECRET", "dashboard-test-secret")


def _payload(agent_id: str = "test-agent") -> dict:
    return {
        "owner_id": "owner-1",
        "agent_id": agent_id,
        "supported_runtime": "perun",
        "scopes": ["pub:global:*"],
        "label": "test",
    }


def _dashboard_headers() -> dict:
    return {"X-Perun-Dashboard-Secret": _DASHBOARD_SECRET}


def _owner_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _create(agent_id: str = "test-agent", owner_id: str = "owner-1") -> tuple[str, dict]:
    """Create a key and return (plaintext_key, key_record)."""
    payload = _payload(agent_id=agent_id)
    payload["owner_id"] = owner_id
    res = client.post("/api/v1/keys", json=payload, headers=_dashboard_headers())
    assert res.status_code == 200
    data = res.json()
    return data["plaintext_key"], data["key"]


def test_create_key_success() -> None:
    res = client.post("/api/v1/keys", json=_payload(), headers=_dashboard_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["plaintext_key"].startswith("prn_live_")
    assert data["key"]["agent_id"] == "test-agent"


def test_create_key_ignores_placeholder_owner_id() -> None:
    """Public dashboard sends 'dashboard-user'; server must generate a real owner_id."""
    payload = _payload()
    payload["owner_id"] = "dashboard-user"
    res = client.post("/api/v1/keys", json=payload, headers=_dashboard_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["owner_id"] != "dashboard-user"
    assert len(data["owner_id"]) >= 16


def test_create_key_allows_public_creation() -> None:
    """Key creation no longer requires the dashboard secret (per-IP rate limit applies)."""
    res = client.post("/api/v1/keys", json=_payload(agent_id="public-agent"))
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["plaintext_key"].startswith("prn_live_")


def test_create_key_duplicate_returns_409() -> None:
    payload = _payload()
    assert client.post("/api/v1/keys", json=payload, headers=_dashboard_headers()).status_code == 200
    res = client.post("/api/v1/keys", json=payload, headers=_dashboard_headers())
    assert res.status_code == 409
    assert "already registered" in res.json()["detail"]


def test_create_key_invalid_agent_id_returns_400() -> None:
    res = client.post("/api/v1/keys", json=_payload(agent_id="ab"), headers=_dashboard_headers())
    assert res.status_code == 400
    assert "agent_id" in res.json()["detail"].lower()


def test_create_key_rate_limited() -> None:
    """Dashboard-authenticated key creation must be globally rate-limited."""
    # Override the security pipeline's creation limiter to a tiny burst.
    from perunchat.gateway import gateway

    gateway.security.creation_limiter = SecurityPipeline().creation_limiter
    gateway.security.creation_limiter.capacity = 2
    gateway.security.creation_limiter.window_seconds = 60
    gateway.security.creation_limiter._failures.clear()
    # First two requests should succeed.
    assert client.post("/api/v1/keys", json=_payload(agent_id="rate-a"), headers=_dashboard_headers()).status_code == 200
    assert client.post("/api/v1/keys", json=_payload(agent_id="rate-b"), headers=_dashboard_headers()).status_code == 200
    # Third request in the same window must be rate limited.
    res = client.post("/api/v1/keys", json=_payload(agent_id="rate-c"), headers=_dashboard_headers())
    assert res.status_code == 429
    assert res.json()["detail"] == "key_creation_rate_limited"


def test_create_key_public_rate_limited() -> None:
    """Public key creation must be rate-limited per IP."""
    from perunchat.gateway import gateway

    gateway.security.public_creation_limiter = SecurityPipeline().public_creation_limiter
    gateway.security.public_creation_limiter.capacity = 2
    gateway.security.public_creation_limiter.window_seconds = 60
    gateway.security.public_creation_limiter._failures.clear()
    # First two public requests should succeed.
    assert client.post("/api/v1/keys", json=_payload(agent_id="public-rate-a")).status_code == 200
    assert client.post("/api/v1/keys", json=_payload(agent_id="public-rate-b")).status_code == 200
    # Third public request from the same IP must be rate limited.
    res = client.post("/api/v1/keys", json=_payload(agent_id="public-rate-c"))
    assert res.status_code == 429
    assert res.json()["detail"] == "key_creation_rate_limited"


def test_check_agent_id_availability() -> None:
    assert (
        client.get("/api/v1/keys/availability/free-agent", headers=_dashboard_headers()).json()["available"] is True
    )
    client.post("/api/v1/keys", json=_payload(agent_id="free-agent"), headers=_dashboard_headers())
    assert (
        client.get("/api/v1/keys/availability/free-agent", headers=_dashboard_headers()).json()["available"] is False
    )


def test_list_keys_requires_owner_authentication() -> None:
    res = client.get("/api/v1/keys", headers=_dashboard_headers())
    assert res.status_code == 401


def test_list_keys_includes_created_for_owner() -> None:
    key, _ = _create(agent_id="listed-agent")
    res = client.get("/api/v1/keys", headers=_owner_headers(key))
    assert res.status_code == 200
    agent_ids = {k["agent_id"] for k in res.json()["keys"]}
    assert "listed-agent" in agent_ids


def test_list_keys_is_scoped_to_owner() -> None:
    key_a, _ = _create(agent_id="owner-a-agent", owner_id="owner-a")
    _create(agent_id="owner-b-agent", owner_id="owner-b")
    res = client.get("/api/v1/keys", headers=_owner_headers(key_a))
    assert res.status_code == 200
    agent_ids = {k["agent_id"] for k in res.json()["keys"]}
    assert "owner-a-agent" in agent_ids
    assert "owner-b-agent" not in agent_ids


def test_revoke_key_requires_owner_authentication() -> None:
    res = client.delete("/api/v1/keys/key_doesnotexist", headers=_dashboard_headers())
    assert res.status_code == 401


def test_revoke_key_returns_404_for_unknown() -> None:
    key, _ = _create()
    res = client.delete("/api/v1/keys/key_doesnotexist", headers=_owner_headers(key))
    assert res.status_code == 404


def test_revoke_key_is_scoped_to_owner() -> None:
    key_a, record_a = _create(agent_id="revoke-a", owner_id="owner-a")
    _, record_b = _create(agent_id="revoke-b", owner_id="owner-b")
    # owner-a cannot revoke owner-b's key
    res = client.delete(f"/api/v1/keys/{record_b['id']}", headers=_owner_headers(key_a))
    assert res.status_code == 404
    # owner-b can revoke their own key
    res = client.delete(f"/api/v1/keys/{record_b['id']}", headers=_owner_headers(_create(agent_id="revoke-b2", owner_id="owner-b")[0]))
    # Note: created a second key for owner-b to authenticate; the original key is still active.
    # Actually the first record_b key is still active, so the second key can revoke it.
    assert res.status_code == 200


def test_me_returns_authenticated_agent() -> None:
    res = client.post("/api/v1/keys", json=_payload(agent_id="me-agent"), headers=_dashboard_headers())
    key = res.json()["plaintext_key"]
    res = client.get("/api/v1/me", headers={"Authorization": f"Bearer {key}"})
    assert res.status_code == 200
    data = res.json()
    assert data["agent_id"] == "me-agent"
    assert "perun" in data["supported_runtime"]


def test_me_requires_authentication() -> None:
    res = client.get("/api/v1/me")
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# Room discovery (GET /api/v1/rooms)
# --------------------------------------------------------------------------- #


def _auth(agent_id: str = "rooms-agent") -> dict:
    """Create a key and return its Authorization header."""
    res = client.post("/api/v1/keys", json=_payload(agent_id=agent_id), headers=_dashboard_headers())
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['plaintext_key']}"}


def test_rooms_requires_authentication() -> None:
    res = client.get("/api/v1/rooms")
    assert res.status_code == 401


def test_rooms_lists_known_public_rooms() -> None:
    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["agent_id"] == "rooms-agent"
    rooms = {r["room_id"]: r for r in data["rooms"]}
    # Well-known public rooms are always discoverable with pub:global:* scopes.
    assert "pub:global:general" in rooms
    assert rooms["pub:global:general"]["type"] == "public"
    assert rooms["pub:global:general"]["active"] is False


def test_rooms_includes_active_rooms() -> None:
    from perunchat.gateway import Room

    gateway._rooms["pub:global:live"] = Room("pub:global:live", "PUBLIC")
    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    rooms = {r["room_id"]: r for r in res.json()["rooms"]}
    assert rooms["pub:global:live"]["active"] is True


def test_rooms_includes_persisted_memberships(tmp_path, monkeypatch) -> None:
    """Persisted room_members rows are surfaced as joined rooms."""
    from perunchat import config as config_module
    from perunchat import db as db_module

    db_file = str(tmp_path / "catalog.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)
    with db_module.db(db_file) as conn:
        conn.execute(
            "INSERT INTO room_members (room_id, agent_id, joined_at) VALUES (?, ?, ?)",
            ("pub:global:alerts", "rooms-agent", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()

    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    rooms = {r["room_id"]: r for r in res.json()["rooms"]}
    assert rooms["pub:global:alerts"]["joined"] is True
    assert rooms["pub:global:alerts"]["members"] == 1


def test_rooms_excludes_out_of_scope_and_foreign_private(tmp_path, monkeypatch) -> None:
    """Rooms the caller's key cannot join are never advertised."""
    from perunchat import config as config_module
    from perunchat import db as db_module
    from perunchat.rooms import private_room_id

    db_file = str(tmp_path / "catalog.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)
    foreign_priv = private_room_id("rooms-agent", "someone-else")
    with db_module.db(db_file) as conn:
        conn.executemany(
            "INSERT INTO room_members (room_id, agent_id, joined_at) VALUES (?, ?, ?)",
            [
                ("pub:other-tenant:internal", "rooms-agent", "2026-01-01T00:00:00+00:00"),
                (foreign_priv, "someone-else", "2026-01-01T00:00:00+00:00"),
            ],
        )
        conn.commit()

    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    ids = {r["room_id"] for r in res.json()["rooms"]}
    assert "pub:other-tenant:internal" not in ids  # scope mismatch
    assert foreign_priv not in ids  # not in the caller's ACL


def test_rooms_honours_public_rooms_env_override(monkeypatch) -> None:
    """PERUNCHAT_PUBLIC_ROOMS replaces the well-known channel list."""
    monkeypatch.setenv("PERUNCHAT_PUBLIC_ROOMS", "pub:global:ops, pub:global:news")
    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    ids = {r["room_id"] for r in res.json()["rooms"]}
    assert {"pub:global:ops", "pub:global:news"} <= ids
    assert "pub:global:general" not in ids  # default replaced, not merged


def test_rooms_empty_public_rooms_env_yields_no_defaults(monkeypatch) -> None:
    """An empty override yields no well-known rooms (no defaults leak in)."""
    monkeypatch.setenv("PERUNCHAT_PUBLIC_ROOMS", "")
    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    assert res.status_code == 200
    assert res.json()["rooms"] == []


def test_rooms_skips_malformed_room_id_rows(tmp_path, monkeypatch) -> None:
    """Corrupt catalog rows are skipped without failing the request."""
    from perunchat import config as config_module
    from perunchat import db as db_module

    db_file = str(tmp_path / "catalog.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)
    with db_module.db(db_file) as conn:
        conn.execute(
            "INSERT INTO room_members (room_id, agent_id, joined_at) VALUES (?, ?, ?)",
            ("not-a-valid-room", "rooms-agent", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()

    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    assert res.status_code == 200
    ids = {r["room_id"] for r in res.json()["rooms"]}
    assert "not-a-valid-room" not in ids


def test_rooms_group_room_joined_flag_via_acl(tmp_path, monkeypatch) -> None:
    """Group rooms appear with joined=True when the caller is in the ACL."""
    from perunchat import config as config_module
    from perunchat import db as db_module
    from perunchat.rooms import group_room_id

    db_file = str(tmp_path / "catalog.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)
    grp = group_room_id("acme", "proj", "standup")
    with db_module.db(db_file) as conn:
        conn.executemany(
            "INSERT INTO room_members (room_id, agent_id, joined_at) VALUES (?, ?, ?)",
            [
                (grp, "rooms-agent", "2026-01-01T00:00:00+00:00"),
                (grp, "teammate", "2026-01-01T00:00:00+00:00"),
            ],
        )
        conn.commit()
    # Mimic the gateway ACL load that runs at startup for group rooms.
    gateway.rooms.add_member(grp, "rooms-agent")
    gateway.rooms.add_member(grp, "teammate")

    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    rooms = {r["room_id"]: r for r in res.json()["rooms"]}
    assert rooms[grp]["type"] == "group"
    assert rooms[grp]["joined"] is True
    assert rooms[grp]["members"] == 2



def test_rooms_includes_online_members_and_count(tmp_path, monkeypatch):
    """Active rooms report the authenticated agents currently connected."""
    from perunchat import config as config_module
    from perunchat import db as db_module
    from perunchat.gateway import Room, gateway

    db_file = str(tmp_path / "online.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)

    room_id = "pub:global:online-api"
    gateway._rooms[room_id] = Room(room_id, "PUBLIC")
    gateway._rooms[room_id].connections["ws1"] = "alice"
    gateway._rooms[room_id].connections["ws2"] = "bob"
    gateway._rooms[room_id].connections["ws3"] = "anon:1.2.3.4"

    headers = _auth()
    res = client.get("/api/v1/rooms", headers=headers)
    rooms = {r["room_id"]: r for r in res.json()["rooms"]}
    assert rooms[room_id]["active"] is True
    assert rooms[room_id]["online_count"] == 2
    assert set(rooms[room_id]["online_members"]) == {"alice", "bob"}


def test_get_room_members_requires_access() -> None:
    """The members endpoint rejects rooms the caller cannot join."""
    key, _ = _create(agent_id="member-check")
    res = client.get("/api/v1/rooms/grp:other:project:room/members", headers=_owner_headers(key))
    assert res.status_code == 403


def test_get_room_members_returns_online_and_persisted(tmp_path, monkeypatch):
    """The members endpoint lists live connections and durable memberships."""
    from perunchat import config as config_module
    from perunchat import db as db_module
    from perunchat.gateway import Room, gateway
    from perunchat.rooms import group_room_id

    db_file = str(tmp_path / "members.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)

    grp = group_room_id("acme", "proj", "standup")
    with db_module.db(db_file) as conn:
        conn.executemany(
            "INSERT INTO room_members (room_id, agent_id, joined_at) VALUES (?, ?, ?)",
            [
                (grp, "rooms-agent", "2026-01-01T00:00:00+00:00"),
                (grp, "offline-member", "2026-01-01T00:00:00+00:00"),
            ],
        )
        conn.commit()
    gateway.rooms.add_member(grp, "rooms-agent")
    gateway.rooms.add_member(grp, "offline-member")
    gateway._rooms[grp] = Room(grp, "GROUP")
    gateway._rooms[grp].connections["ws1"] = "rooms-agent"

    headers = _auth()
    res = client.get(f"/api/v1/rooms/{grp}/members", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["online"] == ["rooms-agent"]
    assert set(data["members"]) == {"rooms-agent", "offline-member"}
