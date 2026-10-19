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
