"""Tests for the PerunChat social graph REST endpoints and gateway integration."""

from __future__ import annotations

import os
import time
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PERUNCHAT_ENV", "test")

import json

from perunchat import api as api_module
from perunchat.gateway import app, gateway
from perunchat.keys import KeyStore
from perunchat.rooms import private_room_id
from perunchat.security import SecurityPipeline
from perunchat.social import SocialGraph


@pytest.fixture(autouse=True)
def reset_gateway(tmp_path):
    if gateway.keystore is not None:
        gateway.keystore.close()
    if api_module.social_graph is not None:
        api_module.social_graph.close()
    db_path = str(tmp_path / "perunchat.db")
    os.environ["PERUNCHAT_DB_PATH"] = db_path
    fresh = KeyStore(db_path)
    gateway.keystore = fresh
    api_module.keystore = fresh
    api_module.social_graph = SocialGraph(db_path)
    gateway.social_graph = api_module.social_graph
    gateway.security = SecurityPipeline()
    gateway.rooms._acls.clear()
    gateway._rooms.clear()
    api_module.proposal_store = api_module.ProposalStore(db_path)
    gateway.proposal_store = api_module.proposal_store


client = TestClient(app)


_DASHBOARD_SECRET = os.environ.get("PERUNCHAT_DASHBOARD_SECRET", "dashboard-test-secret")


def _create_key(agent_id: str, owner_id: str = "owner-1") -> str:
    res = client.post(
        "/api/v1/keys",
        headers={"X-Perun-Dashboard-Secret": _DASHBOARD_SECRET},
        json={
            "owner_id": owner_id,
            "agent_id": agent_id,
            "supported_runtime": "perun",
            "scopes": ["pub:global:*"],
        },
    )
    assert res.status_code == 200
    return res.json()["plaintext_key"]


def _auth(agent_id: str, api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_send_friend_request_requires_auth() -> None:
    res = client.post("/api/v1/social/friends/requests", json={"to_agent_id": "bob"})
    assert res.status_code == 401


def test_send_and_accept_friend_request() -> None:
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")

    res = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 200
    request_id = res.json()["request"]["request_id"]

    # Bob sees one incoming request.
    res = client.get(
        "/api/v1/social/friends/requests?direction=incoming",
        headers=_auth("bob", bob_key),
    )
    assert res.status_code == 200
    assert len(res.json()["requests"]) == 1

    # Alice cannot accept her own request.
    res = client.post(
        f"/api/v1/social/friends/requests/{request_id}/accept",
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 409

    # Bob accepts.
    res = client.post(
        f"/api/v1/social/friends/requests/{request_id}/accept",
        headers=_auth("bob", bob_key),
    )
    assert res.status_code == 200

    # They are now friends.
    res = client.get("/api/v1/social/friends", headers=_auth("alice", alice_key))
    assert res.json()["friends"] == ["bob"]
    res = client.get("/api/v1/social/friends", headers=_auth("bob", bob_key))
    assert res.json()["friends"] == ["alice"]


def test_duplicate_friend_request_returns_409() -> None:
    alice_key = _create_key("alice")
    _create_key("bob")

    client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    res = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 409


def test_friend_request_can_be_re_sent_after_rejection() -> None:
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")

    req = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    ).json()["request"]
    res = client.post(
        f"/api/v1/social/friends/requests/{req['request_id']}/reject",
        headers=_auth("bob", bob_key),
    )
    assert res.status_code == 200

    # A new request after rejection must succeed.
    res = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 200
    assert res.json()["request"]["status"] == "pending"


def test_mixed_case_agent_ids_are_normalized() -> None:
    now_ms = int(time.time() * 1000)
    alice_key = _create_key("AliceInWonderland")
    bob_key = _create_key("BOB_THE_BUILDER")

    request = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "BOB_THE_BUILDER"},
        headers=_auth("aliceinwonderland", alice_key),
    ).json()["request"]
    client.post(
        f"/api/v1/social/friends/requests/{request['request_id']}/accept",
        headers=_auth("bob_the_builder", bob_key),
    )

    # Connect both agents before anyone joins the private room so that
    # presence-update broadcasts go to empty rooms and cannot race the chat
    # message we are about to exchange.
    room_id = private_room_id("AliceInWonderland", "BOB_THE_BUILDER")
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("aliceinwonderland", alice_key)) as ws1:
            with tc.websocket_connect("/v1/chat/ws", headers=_auth("bob_the_builder", bob_key)) as ws2:
                ws1.send_text(json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PRIVATE"}))
                assert json.loads(ws1.receive_text())["type"] == "ROOM_JOINED"

                ws2.send_text(json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PRIVATE"}))
                assert json.loads(ws2.receive_text())["type"] == "ROOM_JOINED"

                ws2.send_text(
                    json.dumps(
                        {
                            "id": "msg_001",
                            "trace_id": "trc_001",
                            "thread_id": "default",
                            "sequence_number": 1,
                            "timestamp": now_ms,
                            "room_type": "PRIVATE",
                            "room_id": room_id,
                            "sender": {"agent_id": "bob", "runtime": "perun"},
                            "content_type": "text/plain",
                            "turn_count": 0,
                            "payload": "hello alice",
                        }
                    )
                )
                msg = json.loads(ws1.receive_text())
                assert msg["sender"]["agent_id"] == "bob_the_builder"


def test_remove_friend() -> None:
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")

    request = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    ).json()["request"]
    client.post(
        f"/api/v1/social/friends/requests/{request['request_id']}/accept",
        headers=_auth("bob", bob_key),
    )

    res = client.delete(
        "/api/v1/social/friends/bob",
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 200
    res = client.get("/api/v1/social/friends", headers=_auth("alice", alice_key))
    assert res.json()["friends"] == []


def test_block_dissolves_friendship() -> None:
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")

    request = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    ).json()["request"]
    client.post(
        f"/api/v1/social/friends/requests/{request['request_id']}/accept",
        headers=_auth("bob", bob_key),
    )

    res = client.post(
        "/api/v1/social/blocked",
        json={"blocked_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 200

    # Friendship is gone.
    res = client.get("/api/v1/social/friends", headers=_auth("alice", alice_key))
    assert res.json()["friends"] == []

    # Alice sees Bob in her blocked list.
    res = client.get("/api/v1/social/blocked", headers=_auth("alice", alice_key))
    assert res.json()["blocked"] == ["bob"]


def test_unblock() -> None:
    alice_key = _create_key("alice")
    _create_key("bob")
    client.post(
        "/api/v1/social/blocked",
        json={"blocked_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    res = client.delete(
        "/api/v1/social/blocked/bob",
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 200
    res = client.get("/api/v1/social/blocked", headers=_auth("alice", alice_key))
    assert res.json()["blocked"] == []


def test_block_prevents_friend_request() -> None:
    alice_key = _create_key("alice")
    _create_key("bob")
    client.post(
        "/api/v1/social/blocked",
        json={"blocked_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    res = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )
    assert res.status_code == 409


def test_friends_status_endpoint() -> None:
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")

    request = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    ).json()["request"]
    client.post(
        f"/api/v1/social/friends/requests/{request['request_id']}/accept",
        headers=_auth("bob", bob_key),
    )

    # Initially nobody is connected via WebSocket, so both offline.
    res = client.get("/api/v1/social/friends/status", headers=_auth("alice", alice_key))
    assert res.status_code == 200
    friends = {f["agent_id"]: f["status"] for f in res.json()["friends"]}
    assert friends.get("bob") == "offline"

    # Simulate Alice connecting to the gateway.
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws:
            res = client.get(
                "/api/v1/social/friends/status",
                headers=_auth("bob", bob_key),
            )
            friends = {f["agent_id"]: f["status"] for f in res.json()["friends"]}
            assert friends.get("alice") == "online"


def test_emoji_payload_passes_through() -> None:
    now_ms = int(time.time() * 1000)
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")
    room_id = "pub:global:test"
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws1:
            ws1.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": "PUBLIC",
                    }
                )
            )
            json.loads(ws1.receive_text())  # ROOM_JOINED
            with tc.websocket_connect("/v1/chat/ws", headers=_auth("bob", bob_key)) as ws2:
                ws2.send_text(
                    json.dumps(
                        {
                            "type": "ROOM_JOIN",
                            "room_id": room_id,
                            "room_type": "PUBLIC",
                        }
                    )
                )
                json.loads(ws2.receive_text())  # ROOM_JOINED

                ws1.send_text(
                    json.dumps(
                        {
                            "id": "msg_emoji",
                            "trace_id": "trc_emoji",
                            "thread_id": "default",
                            "sequence_number": 1,
                            "timestamp": now_ms,
                            "room_type": "PUBLIC",
                            "room_id": room_id,
                            "sender": {"agent_id": "alice", "runtime": "perun"},
                            "content_type": "text/plain",
                            "turn_count": 0,
                            "payload": "Hello 👋 🌍",
                        }
                    )
                )
                received = json.loads(ws2.receive_text())
                assert received["payload"] == "Hello 👋 🌍"


def test_reply_in_reply_to_passthrough() -> None:
    now_ms = int(time.time() * 1000)
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")
    room_id = "pub:global:test"
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws1:
            ws1.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": "PUBLIC",
                    }
                )
            )
            json.loads(ws1.receive_text())
            with tc.websocket_connect("/v1/chat/ws", headers=_auth("bob", bob_key)) as ws2:
                ws2.send_text(
                    json.dumps(
                        {
                            "type": "ROOM_JOIN",
                            "room_id": room_id,
                            "room_type": "PUBLIC",
                        }
                    )
                )
                json.loads(ws2.receive_text())

                # Alice sends a message.
                ws1.send_text(
                    json.dumps(
                        {
                            "id": "msg_original",
                            "trace_id": "trc_1",
                            "thread_id": "default",
                            "sequence_number": 1,
                            "timestamp": now_ms,
                            "room_type": "PUBLIC",
                            "room_id": room_id,
                            "sender": {"agent_id": "alice", "runtime": "perun"},
                            "content_type": "text/plain",
                            "turn_count": 0,
                            "payload": "original",
                        }
                    )
                )
                # The gateway echoes the sender's own message back.
                echo = json.loads(ws1.receive_text())
                assert echo["payload"] == "original"
                json.loads(ws2.receive_text())

                # Bob replies to Alice's message.
                ws2.send_text(
                    json.dumps(
                        {
                            "id": "msg_reply",
                            "trace_id": "trc_2",
                            "thread_id": "default",
                            "sequence_number": 1,
                            "timestamp": now_ms,
                            "room_type": "PUBLIC",
                            "room_id": room_id,
                            "sender": {"agent_id": "bob", "runtime": "perun"},
                            "in_reply_to": "msg_original",
                            "content_type": "text/plain",
                            "turn_count": 0,
                            "payload": "reply",
                        }
                    )
                )
                # Bob receives his own echo; Alice receives the actual reply.
                bob_echo = json.loads(ws2.receive_text())
                assert bob_echo["payload"] == "reply"
                received = json.loads(ws1.receive_text())
                assert received["payload"] == "reply"
                assert received["in_reply_to"] == "msg_original"


def test_public_room_history_replay_for_late_joiner() -> None:
    now_ms = int(time.time() * 1000)
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")
    room_id = "pub:global:test"
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws1:
            ws1.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": "PUBLIC",
                    }
                )
            )
            json.loads(ws1.receive_text())  # ROOM_JOINED

            ws1.send_text(
                json.dumps(
                    {
                        "id": "msg_hist_1",
                        "trace_id": "trc_1",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "alice", "runtime": "perun"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "first",
                    }
                )
            )
            # Broadcast skips the sender, so Alice gets nothing here.

            # Bob joins later and should receive the history replay after ROOM_JOINED.
            with tc.websocket_connect("/v1/chat/ws", headers=_auth("bob", bob_key)) as ws2:
                ws2.send_text(
                    json.dumps(
                        {
                            "type": "ROOM_JOIN",
                            "room_id": room_id,
                            "room_type": "PUBLIC",
                        }
                    )
                )
                ack = json.loads(ws2.receive_text())
                assert ack["type"] == "ROOM_JOINED"

                replay = json.loads(ws2.receive_text())
                assert replay["payload"] == "first"


def test_oversized_payload_is_rejected() -> None:
    now_ms = int(time.time() * 1000)
    alice_key = _create_key("alice")
    room_id = "pub:global:test"
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": "PUBLIC",
                    }
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_big",
                        "trace_id": "trc_big",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "PUBLIC",
                        "room_id": room_id,
                        "sender": {"agent_id": "alice", "runtime": "perun"},
                        "content_type": "text/plain",
                        "turn_count": 0,
                        "payload": "x" * 10000,
                    }
                )
            )
            error = json.loads(ws.receive_text())
            assert error["type"] == "ERROR"
            assert error["message"] == "payload_too_large"


def test_friends_can_join_private_room() -> None:
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")

    request = client.post(
        "/api/v1/social/friends/requests",
        json={"to_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    ).json()["request"]
    client.post(
        f"/api/v1/social/friends/requests/{request['request_id']}/accept",
        headers=_auth("bob", bob_key),
    )

    room_id = private_room_id("alice", "bob")
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws1:
            ws1.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": "PRIVATE",
                    }
                )
            )
            assert json.loads(ws1.receive_text())["type"] == "ROOM_JOINED"
            with tc.websocket_connect("/v1/chat/ws", headers=_auth("bob", bob_key)) as ws2:
                ws2.send_text(
                    json.dumps(
                        {
                            "type": "ROOM_JOIN",
                            "room_id": room_id,
                            "room_type": "PRIVATE",
                        }
                    )
                )
                assert json.loads(ws2.receive_text())["type"] == "ROOM_JOINED"


def test_non_friends_cannot_join_private_room() -> None:
    alice_key = _create_key("alice")
    _create_key("bob")
    room_id = private_room_id("alice", "bob")
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/chat/ws", headers=_auth("alice", alice_key)) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "type": "ROOM_JOIN",
                        "room_id": room_id,
                        "room_type": "PRIVATE",
                    }
                )
            )
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "ERROR"
            assert ack["message"] == "room_access_denied"


def test_blocked_messages_are_not_delivered() -> None:
    """Unit test: Room.broadcast skips a recipient who has blocked the sender."""
    import asyncio

    from perunchat.gateway import Room

    # Establish a block so social_graph.is_blocked(alice, bob) is True.
    alice_key = _create_key("alice")
    bob_key = _create_key("bob")
    client.post(
        "/api/v1/social/blocked",
        json={"blocked_agent_id": "bob"},
        headers=_auth("alice", alice_key),
    )

    room = Room("pub:global:test", "PUBLIC")

    class FakeWs:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    alice_ws = FakeWs()
    bob_ws = FakeWs()
    room.connections[alice_ws] = "alice"
    room.connections[bob_ws] = "bob"

    async def run_broadcast() -> None:
        await room.broadcast(
            bob_ws,
            {
                "id": "msg_1",
                "sender": {"agent_id": "bob"},
                "content_type": "text/plain",
                "payload": "hello",
            },
        )

    asyncio.run(run_broadcast())
    assert len(bob_ws.sent) == 1  # sender now receives own message echo
    assert json.loads(bob_ws.sent[0])["payload"] == "hello"
    assert len(alice_ws.sent) == 0  # blocked by Alice -> not delivered
