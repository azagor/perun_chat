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
        "live": False,
    }


def _dashboard_headers() -> dict:
    return {"X-Perun-Dashboard-Secret": _DASHBOARD_SECRET}


def test_create_key_success() -> None:
    res = client.post("/api/v1/keys", json=_payload(), headers=_dashboard_headers())
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["plaintext_key"].startswith("prn_test_")
    assert data["key"]["agent_id"] == "test-agent"


def test_create_key_requires_dashboard_secret() -> None:
    res = client.post("/api/v1/keys", json=_payload())
    assert res.status_code == 403


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


def test_check_agent_id_availability() -> None:
    assert (
        client.get("/api/v1/keys/availability/free-agent", headers=_dashboard_headers()).json()["available"] is True
    )
    client.post("/api/v1/keys", json=_payload(agent_id="free-agent"), headers=_dashboard_headers())
    assert (
        client.get("/api/v1/keys/availability/free-agent", headers=_dashboard_headers()).json()["available"] is False
    )


def test_revoke_key_returns_404_for_unknown() -> None:
    res = client.delete("/api/v1/keys/key_doesnotexist", headers=_dashboard_headers())
    assert res.status_code == 404


def test_list_keys_includes_created() -> None:
    client.post("/api/v1/keys", json=_payload(agent_id="listed-agent"), headers=_dashboard_headers())
    res = client.get("/api/v1/keys", headers=_dashboard_headers())
    assert res.status_code == 200
    agent_ids = {k["agent_id"] for k in res.json()["keys"]}
    assert "listed-agent" in agent_ids


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
