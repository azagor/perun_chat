"""Tests for the PerunChat gateway."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import os
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
    gateway.keystore = KeyStore(db_path)
    api_module.keystore = gateway.keystore
    api_module.social_graph = SocialGraph(db_path)
    gateway.social_graph = api_module.social_graph
    gateway.security = SecurityPipeline()
    gateway.rooms._acls.clear()
    gateway._rooms.clear()
    api_module.pending_proposals.clear()


def _headers(key: str, agent_id: str = "test-agent", runtime: str = "custom") -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "X-Agent-ID": agent_id,
        "X-Agent-Runtime": runtime,
    }


def _seed_key() -> str:
    raw, _ = gateway.keystore.create(
        owner_id="owner-1",
        agent_id="test-agent",
        supported_runtime="custom",
        scopes=["grp:test:room"],
    )
    return raw


def test_health_endpoint() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["service"] == "perunchat-gateway"


def test_anonymous_can_join_public_room_and_is_read_only() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": "pub:global:general",
                        "room_type": "PUBLIC",
                    }
                )
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ROOM_JOINED"

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_001",
                        "trace_id": "trc_001",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": 1,
                        "room_type": "PUBLIC",
                        "room_id": "pub:global:general",
                        "sender": {"agent_id": "anon", "runtime": "anonymous"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "hello",
                    }
                )
            )
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert err["message"] == "anonymous_read_only"


def test_anonymous_cannot_join_group_room() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": "grp:test:room",
                        "room_type": "GROUP",
                    }
                )
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ERROR"
            assert ack["message"] == "room_access_denied"


def test_websocket_join_room() -> None:
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": "grp:test:room",
                        "room_type": "GROUP",
                    }
                )
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ROOM_JOINED"


def test_websocket_rejects_mismatched_agent_id() -> None:
    key = _seed_key()
    with TestClient(app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/v1/chat/ws", headers=_headers(key, agent_id="imposter-agent")
            ) as ws:
                pass


def test_websocket_rejects_unscoped_group_room() -> None:
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": "grp:other:project:room",
                        "room_type": "GROUP",
                    }
                )
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ERROR"
            assert ack["message"] == "room_access_denied"


def test_websocket_uses_key_agent_id_when_header_omitted() -> None:
    key = _seed_key()
    headers = {"Authorization": f"Bearer {key}", "X-Agent-Runtime": "custom"}
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=headers) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": "grp:test:room",
                        "room_type": "GROUP",
                    }
                )
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ROOM_JOINED"


def test_websocket_rejects_proposal_without_approval_token() -> None:
    import time

    key = _seed_key()
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": "grp:test:room",
                        "room_type": "GROUP",
                    }
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_001",
                        "trace_id": "trc_001",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "GROUP",
                        "room_id": "grp:test:room",
                        "sender": {"agent_id": "test-agent", "runtime": "custom"},
                        "content_type": "application/perun-proposal",
                        "turn_count": 0,
                        "payload": {
                            "proposal_id": "prop_001",
                            "proposal_type": "PERUN_ACTION_PROPOSAL",
                            "risk_tier": "TIER_2_LOW_IMPACT",
                            "action_domain": "shell",
                            "description": "Run echo hello",
                            "execution_details": {"command": "echo hello"},
                            "provenance": {
                                "instigator": "test-agent",
                                "justification": "demo",
                            },
                        },
                    }
                )
            )
            error = json.loads(ws.receive_text())
            assert error["type"] == "ERROR"
            assert error["message"] == "proposal_missing_approval_token"


def _headers_optional_runtime(key: str, agent_id: str = "test-agent", runtime: str | None = "custom") -> dict:
    headers = {
        "Authorization": f"Bearer {key}",
        "X-Agent-ID": agent_id,
    }
    if runtime is not None:
        headers["X-Agent-Runtime"] = runtime
    return headers


def test_runtime_from_key_record_is_preserved_in_echo() -> None:
    """Browser clients cannot set X-Agent-Runtime, so the gateway must use the
    API key's registered runtime and preserve it in the echo/broadcast."""
    import time
    raw, _ = gateway.keystore.create(
        owner_id="owner-runtime",
        agent_id="operator-agent",
        supported_runtime="perun-operator",
        scopes=["pub:global:runtime"],
    )
    room_id = "pub:global:runtime"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "operator-agent", None)
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_operator",
                        "trace_id": "trc_operator",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "operator-agent", "runtime": "perun-operator"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "operator here",
                    }
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["payload"] == "operator here"
            assert echo["sender"]["runtime"] == "operator-agent-operator"


def test_operator_runtime_normalized_from_perun_key() -> None:
    """A dashboard may reuse an autonomous 'perun' key to send operator frames.
    The gateway must detect the client-side operator marker and normalize the
    broadcast/runtime to '<agent_id>-operator' so history replay stays labeled."""
    import time
    raw, _ = gateway.keystore.create(
        owner_id="owner-operator-from-perun",
        agent_id="perun-agent",
        supported_runtime="perun",
        scopes=["pub:global:operatornorm"],
    )
    room_id = "pub:global:operatornorm"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "perun-agent", None)
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_operator_norm",
                        "trace_id": "trc_operator_norm",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "perun-agent", "runtime": "perun-operator"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "operator via perun key",
                    }
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["payload"] == "operator via perun key"
            assert echo["sender"]["runtime"] == "perun-agent-operator"

            # Reconnecting client should see the same operator runtime in replay.
            with client.websocket_connect(
                "/v1/chat/ws", headers=_headers_optional_runtime(raw, "perun-agent", None)
            ) as ws2:
                ws2.send_text(
                    json.dumps(
                        {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                    )
                )
                json.loads(ws2.receive_text())  # ROOM_JOINED
                replay = json.loads(ws2.receive_text())
                assert replay["sender"]["runtime"] == "perun-agent-operator"


def test_explicit_runtime_header_used_for_generic_key() -> None:
    """Autonomous clients with a generic/custom key may declare a specific
    runtime via the X-Agent-Runtime header."""
    import time
    raw, _ = gateway.keystore.create(
        owner_id="owner-generic",
        agent_id="generic-agent",
        supported_runtime="custom",
        scopes=["pub:global:generic"],
    )
    room_id = "pub:global:generic"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "generic-agent", "openclaw")
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_generic",
                        "trace_id": "trc_generic",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "generic-agent", "runtime": "openclaw"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "openclaw here",
                    }
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["payload"] == "openclaw here"
            assert echo["sender"]["runtime"] == "openclaw"


def test_history_replay_preserves_original_id_and_runtime() -> None:
    """Late joiners must receive history with the original message id and
    sender runtime so clients can deduplicate by id without content heuristics."""
    import time
    alice_key = gateway.keystore.create(
        owner_id="owner-alice",
        agent_id="alice",
        supported_runtime="perun",
        scopes=["pub:global:replay"],
    )[0]
    bob_key = gateway.keystore.create(
        owner_id="owner-bob",
        agent_id="bob",
        supported_runtime="perun",
        scopes=["pub:global:replay"],
    )[0]
    room_id = "pub:global:replay"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(alice_key, "alice", "perun")
        ) as ws1:
            ws1.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws1.receive_text())  # ROOM_JOINED

            ws1.send_text(
                json.dumps(
                    {
                        "id": "msg_stable_42",
                        "trace_id": "trc_stable",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "alice", "runtime": "perun"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "stable id",
                    }
                )
            )
            json.loads(ws1.receive_text())  # own echo

            with client.websocket_connect(
                "/v1/chat/ws", headers=_headers_optional_runtime(bob_key, "bob", "perun")
            ) as ws2:
                ws2.send_text(
                    json.dumps(
                        {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                    )
                )
                ack = json.loads(ws2.receive_text())
                assert ack["type"] == "ROOM_JOINED"

                replay = json.loads(ws2.receive_text())
                assert replay["id"] == "msg_stable_42"
                assert replay["payload"] == "stable id"
                assert replay["sender"]["agent_id"] == "alice"
                assert replay["sender"]["runtime"] == "perun"


def test_concrete_runtime_key_ignores_mismatched_header() -> None:
    """A key with a concrete supported_runtime is authoritative; a client cannot
    override it via X-Agent-Runtime."""
    import time
    raw, _ = gateway.keystore.create(
        owner_id="owner-concrete",
        agent_id="concrete-agent",
        supported_runtime="perun",
        scopes=["pub:global:concrete"],
    )
    room_id = "pub:global:concrete"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "concrete-agent", "openclaw")
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_concrete",
                        "trace_id": "trc_concrete",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "concrete-agent", "runtime": "openclaw"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "concrete runtime",
                    }
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["payload"] == "concrete runtime"
            # The key's supported_runtime wins over the header.
            assert echo["sender"]["runtime"] == "perun"


def test_explicit_runtime_header_used_for_generic_key() -> None:
    """Autonomous clients with a generic/custom key may declare a specific
    runtime via the X-Agent-Runtime header."""
    import time
    raw, _ = gateway.keystore.create(
        owner_id="owner-generic",
        agent_id="generic-agent",
        supported_runtime="custom",
        scopes=["pub:global:generic"],
    )
    room_id = "pub:global:generic"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "generic-agent", "openclaw")
        ) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_generic",
                        "trace_id": "trc_generic",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "generic-agent", "runtime": "openclaw"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "openclaw here",
                    }
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["payload"] == "openclaw here"
            assert echo["sender"]["runtime"] == "openclaw"


def test_history_replay_preserves_original_id_and_runtime() -> None:
    """Late joiners must receive history with the original message id and
    sender runtime so clients can deduplicate by id without content heuristics."""
    import time
    alice_key = gateway.keystore.create(
        owner_id="owner-alice",
        agent_id="alice",
        supported_runtime="perun",
        scopes=["pub:global:replay"],
    )[0]
    bob_key = gateway.keystore.create(
        owner_id="owner-bob",
        agent_id="bob",
        supported_runtime="perun",
        scopes=["pub:global:replay"],
    )[0]
    room_id = "pub:global:replay"
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(alice_key, "alice", "perun")
        ) as ws1:
            ws1.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                )
            )
            json.loads(ws1.receive_text())  # ROOM_JOINED

            ws1.send_text(
                json.dumps(
                    {
                        "id": "msg_stable_42",
                        "trace_id": "trc_stable",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "alice", "runtime": "perun"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "stable id",
                    }
                )
            )
            json.loads(ws1.receive_text())  # own echo

            with client.websocket_connect(
                "/v1/chat/ws", headers=_headers_optional_runtime(bob_key, "bob", "perun")
            ) as ws2:
                ws2.send_text(
                    json.dumps(
                        {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"}
                    )
                )
                ack = json.loads(ws2.receive_text())
                assert ack["type"] == "ROOM_JOINED"

                replay = json.loads(ws2.receive_text())
                assert replay["id"] == "msg_stable_42"
                assert replay["payload"] == "stable id"
                assert replay["sender"]["agent_id"] == "alice"
                assert replay["sender"]["runtime"] == "perun"




def test_public_room_membership_removed_on_leave(monkeypatch) -> None:
    """Leaving a public room must remove the persisted membership."""
    from perunchat import config as config_module
    from perunchat import db as db_module

    db_file = str(gateway.keystore.db_path)
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)

    raw, _ = gateway.keystore.create(
        owner_id="owner-leave",
        agent_id="leaver-agent",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    room_id = "pub:global:leave-test"
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "leaver-agent", "perun")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            # Membership was persisted on join.
            assert api_module._is_room_member(room_id, "leaver-agent")

            ws.send_text(
                json.dumps({"type": "ROOM_LEAVE", "room_id": room_id, "room_type": "PUBLIC"})
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ROOM_LEFT"

    # After leaving, the agent is no longer in the room catalog.
    assert not api_module._is_room_member(room_id, "leaver-agent")


def test_public_room_membership_removed_on_disconnect(monkeypatch) -> None:
    """A dropped public-room connection must not leave the agent joined forever."""
    from perunchat import config as config_module
    from perunchat import db as db_module

    db_file = str(gateway.keystore.db_path)
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)

    raw, _ = gateway.keystore.create(
        owner_id="owner-drop",
        agent_id="drop-agent",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    room_id = "pub:global:drop-test"
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "drop-agent", "perun")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED
            assert api_module._is_room_member(room_id, "drop-agent")
        # websocket disconnects here

    assert not api_module._is_room_member(room_id, "drop-agent")


def test_room_online_agent_ids_excludes_anonymous() -> None:
    """Room.online_agent_ids() returns only authenticated agents."""
    from perunchat.gateway import Room

    room = Room("pub:global:online", "PUBLIC")
    room.connections["ws1"] = "alice"
    room.connections["ws2"] = "bob"
    room.connections["ws3"] = "anon:1.2.3.4"
    assert room.online_agent_ids() == {"alice", "bob"}
