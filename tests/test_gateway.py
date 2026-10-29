"""Tests for the PerunChat gateway."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

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
    os.environ["PERUNCHAT_DB_PATH"] = db_path
    gateway.keystore = KeyStore(db_path)
    api_module.keystore = gateway.keystore
    api_module.social_graph = SocialGraph(db_path)
    gateway.social_graph = api_module.social_graph
    gateway.security = SecurityPipeline()
    gateway.rooms._acls.clear()
    gateway._rooms.clear()
    api_module.proposal_store = api_module.ProposalStore(db_path)
    gateway.proposal_store = api_module.proposal_store
    api_module.hitl_config.db_path = db_path
    api_module.hitl_engine = api_module.HITLTokenEngine(api_module.hitl_config)
    gateway.hitl_engine = api_module.hitl_engine


def _headers(key: str, agent_id: str = "test-agent", runtime: str = "custom") -> dict:
    return {
        "Authorization": f"Bearer {key}",
        "X-Agent-ID": agent_id,
        "X-Agent-Runtime": runtime,
    }


def _seed_key(
    scopes: list[str] | None = None, agent_id: str = "test-agent"
) -> str:
    raw, _ = gateway.keystore.create(
        owner_id="owner-1",
        agent_id=agent_id,
        supported_runtime="custom",
        scopes=scopes or ["grp:test:room", "pub:*"],
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


def test_websocket_stages_proposal_without_approval_token() -> None:
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
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "PROPOSAL_STAGED"
            assert ack["proposal_id"] == "prop_001"
            assert api_module.proposal_store.get("prop_001") is not None


def test_websocket_full_two_phase_proposal_lifecycle() -> None:
    import time

    agent_key = _seed_key()
    # Operator with a different agent_id approves the proposal.
    operator_raw, _ = gateway.keystore.create(
        owner_id="owner-op",
        agent_id="operator-agent",
        supported_runtime="custom",
        scopes=["grp:test:room"],
    )
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        # Agent joins and stages a proposal.
        with client.websocket_connect("/v1/chat/ws", headers=_headers(agent_key)) as ws:
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
                        "id": "msg_phase1",
                        "trace_id": "trc_phase1",
                        "thread_id": "default",
                        "sequence_number": 1,
                        "timestamp": now_ms,
                        "room_type": "GROUP",
                        "room_id": "grp:test:room",
                        "sender": {"agent_id": "test-agent", "runtime": "custom"},
                        "content_type": "application/perun-proposal",
                        "turn_count": 0,
                        "payload": {
                            "proposal_id": "prop_lifecycle",
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
            ack = json.loads(ws.receive_text())
            assert ack["type"] == "PROPOSAL_STAGED"

        # Operator approves via REST.
        approve_resp = client.post(
            "/api/v1/proposals/prop_lifecycle/approve",
            json={"execution_details": {"command": "echo hello"}},
            headers={"Authorization": f"Bearer {operator_raw}"},
        )
        assert approve_resp.status_code == 200
        token = approve_resp.json()["approval_token"]
        assert token["proposal_id"] == "prop_lifecycle"

        # Agent resubmits the proposal with the approval token.
        with client.websocket_connect("/v1/chat/ws", headers=_headers(agent_key)) as ws:
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
                        "id": "msg_phase2",
                        "trace_id": "trc_phase2",
                        "thread_id": "default",
                        "sequence_number": 2,
                        "timestamp": now_ms + 1,
                        "room_type": "GROUP",
                        "room_id": "grp:test:room",
                        "sender": {"agent_id": "test-agent", "runtime": "custom"},
                        "content_type": "application/perun-proposal",
                        "turn_count": 0,
                        "payload": {
                            "proposal_id": "prop_lifecycle",
                            "proposal_type": "PERUN_ACTION_PROPOSAL",
                            "risk_tier": "TIER_2_LOW_IMPACT",
                            "action_domain": "shell",
                            "description": "Run echo hello",
                            "execution_details": {"command": "echo hello"},
                            "provenance": {
                                "instigator": "test-agent",
                                "justification": "demo",
                            },
                            "approval_token": token["token"],
                            "approval_expiry_epoch": token["expiry_epoch"],
                            "approval_nonce": token["nonce"],
                        },
                    }
                )
            )
            broadcast = json.loads(ws.receive_text())
            assert broadcast["content_type"] == "application/perun-proposal"
            assert broadcast["payload"]["proposal_id"] == "prop_lifecycle"
            assert broadcast["payload"].get("approval_token")
            assert api_module.proposal_store.get("prop_lifecycle") is None


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


def _join_room(ws, room_id: str = "grp:test:room") -> None:
    ws.send_text(
        json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
    )
    assert json.loads(ws.receive_text())["type"] == "ROOM_JOINED"


def _message(
    payload: Any,
    room_id: str = "grp:test:room",
    content_type: str = "text/plain",
    seq: int = 1,
    **over: Any,
) -> str:
    """Build a full MESSAGE envelope with all required ChatFrame fields."""
    frame = {
        "type": "MESSAGE",
        "id": f"msg_{seq}",
        "trace_id": "trc_test",
        "thread_id": "default",
        "sequence_number": seq,
        "timestamp": int(time.time() * 1000),
        "room_type": "GROUP",
        "room_id": room_id,
        "sender": {"agent_id": "test-agent", "runtime": "custom"},
        "content_type": content_type,
        "turn_count": 0,
        "payload": payload,
    }
    frame.update(over)
    return json.dumps(frame)


class FakeWS:
    """Async WebSocket stand-in for room-level unit tests."""

    def __init__(self, stall: Any = None, raise_on: int | None = None) -> None:
        self.sent: list[Dict[str, Any]] = []
        self.stall = stall
        self._raise_on = raise_on
        self._calls = 0
        self.closed = False

    async def send_text(self, text: str) -> None:
        self._calls += 1
        if self.stall is not None:
            await self.stall
        if self._raise_on is not None and self._calls >= self._raise_on:
            raise RuntimeError("simulated send failure")
        await asyncio.sleep(0)
        self.sent.append(json.loads(text))

    async def close(self, *args, **kwargs) -> None:
        self.closed = True


def _frame(idx: int, content_type: str = "text/plain") -> Dict[str, Any]:
    return {
        "id": f"m{idx}",
        "trace_id": "trc",
        "thread_id": "default",
        "sequence_number": idx,
        "timestamp": idx,
        "room_type": "GROUP",
        "room_id": "grp:test:room",
        "sender": {"agent_id": "alice", "runtime": "custom"},
        "content_type": content_type,
        "turn_count": 0,
        "payload": str(idx),
    }


def _room(monkeypatch, n: int = 500) -> Any:
    """Create a Room filled with *n* stored frames."""
    from perunchat.gateway import Room

    monkeypatch.setattr("perunchat.gateway.gateway", SimpleNamespace(social_graph=None))
    room = Room("grp:test:room", "GROUP")

    async def fill() -> None:
        for i in range(n):
            await room.broadcast(None, _frame(i))

    asyncio.run(fill())
    return room


def test_replay_is_ordered_and_live_frame_arrives_once(monkeypatch) -> None:
    """A late joiner receives history in order and a live frame sent mid-replay arrives exactly once, after it."""
    room = _room(monkeypatch)

    async def scenario() -> list[Dict[str, Any]]:
        late, other = FakeWS(), FakeWS()
        await room.join(other, "alice")
        await room.join(late, "bob")
        replay = asyncio.create_task(room.replay_history(late, "bob"))
        for _ in range(5):
            await asyncio.sleep(0)
        await room.broadcast(other, _frame(9999))
        await replay
        return late.sent

    got = [f["id"] for f in asyncio.run(scenario())]
    assert got == [f"m{i}" for i in range(500)] + ["m9999"]


def test_full_ring_buffer_does_not_truncate_replay(monkeypatch) -> None:
    """When history is at the 500 cap, replay still completes without duplicates."""
    from perunchat.gateway import _MAX_ROOM_HISTORY

    room = _room(monkeypatch, n=_MAX_ROOM_HISTORY)

    async def scenario() -> list[str]:
        late, other = FakeWS(), FakeWS()
        await room.join(other, "alice")
        await room.join(late, "bob")
        replay = asyncio.create_task(room.replay_history(late, "bob"))
        await asyncio.sleep(0)
        for i in range(3):
            await room.broadcast(other, _frame(9000 + i))
        await replay
        return [f["id"] for f in late.sent]

    got = asyncio.run(scenario())
    assert len(got) == len(set(got))
    assert got[-3:] == ["m9000", "m9001", "m9002"]


def test_stalled_client_does_not_block_room(monkeypatch) -> None:
    """One stalled client must not delay delivery to others."""
    from perunchat.gateway import Room

    monkeypatch.setattr("perunchat.gateway.gateway", SimpleNamespace(social_graph=None))
    room = Room("grp:test:room", "GROUP")

    async def scenario() -> tuple[list[Dict[str, Any]], bool]:
        stalled = FakeWS(stall=asyncio.Event())  # never releases
        fast = FakeWS()
        sender = FakeWS()
        for ws_obj, a in ((stalled, "carol"), (fast, "bob"), (sender, "alice")):
            await room.join(ws_obj, a)
        # Mark both recipient sockets as finished replaying so the broadcast
        # targets them directly. The stalled socket will fail to receive, while
        # the fast socket must still get the frame promptly.
        room._replaying.discard(fast)
        room._pending.pop(fast, None)
        room._replaying.discard(stalled)
        room._pending.pop(stalled, None)
        await asyncio.wait_for(room.broadcast(sender, _frame(1)), timeout=10.0)
        return fast.sent, stalled in room.connections

    fast_sent, stalled_still_in = asyncio.run(scenario())
    assert [f["id"] for f in fast_sent] == ["m1"]
    assert stalled_still_in is False


def test_leave_mid_replay_stops_sending_and_clears_state(monkeypatch) -> None:
    """If a socket leaves mid-replay it must not keep receiving history."""
    room = _room(monkeypatch)

    async def scenario() -> tuple[FakeWS, int]:
        late, other = FakeWS(), FakeWS()
        await room.join(other, "alice")
        await room.join(late, "bob")
        replay = asyncio.create_task(room.replay_history(late, "bob"))
        for _ in range(5):
            await asyncio.sleep(0)
        await room.leave(late)
        sent_at_leave = len(late.sent)
        await room.broadcast(other, _frame(9999))
        await replay
        return late, sent_at_leave

    late, at_leave = asyncio.run(scenario())
    assert len(late.sent) <= at_leave + 1
    assert "m9999" not in [f["id"] for f in late.sent]
    assert late not in room._pending
    assert late not in room._replaying
    assert late not in room.connections


@pytest.mark.parametrize("raw", ["[]", "1", '"x"', "null", "true"])
def test_non_object_envelope_gets_error_and_socket_stays_open(raw: str) -> None:
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            ws.send_text(raw)
            reply = json.loads(ws.receive_text())
            assert reply == {"type": "ERROR", "message": "invalid_envelope"}
            _join_room(ws)


@pytest.mark.parametrize("room_id", [5, [], {}, None])
def test_non_string_room_id_gets_error_and_socket_stays_open(room_id: object) -> None:
    """A non-string room_id must not crash the receive loop."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            ws.send_text(
                json.dumps(
                    {"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"}
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "ERROR"
            _join_room(ws)


def test_oversized_raw_frame_closes_with_1009() -> None:
    from perunchat import gateway as gw
    from starlette.websockets import WebSocketDisconnect

    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            ws.send_text("x" * (gw.MAX_FRAME_BYTES + 1))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 1009


def test_payload_boundary_counts_utf8_bytes() -> None:
    """The 8 KiB payload limit counts bytes, not Unicode characters."""
    key = _seed_key()
    ok = "ą" * 4096  # 2 bytes each = 8192 bytes
    bad = ok + "a"  # 8193 bytes
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(_message(bad, seq=1))
            reply = json.loads(ws.receive_text())
            assert reply["message"] == "payload_too_large"
            assert reply["max_bytes"] == 8192

            ws.send_text(_message(ok, seq=2))
            echo = json.loads(ws.receive_text())
            assert echo.get("payload") == ok


def test_package_frame_in_non_managed_room_is_rejected() -> None:
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(_message("{}", seq=1, content_type="application/perun-package"))
            reply = json.loads(ws.receive_text())
            assert reply["message"] == "package_frames_project_rooms_only"


def test_deeply_nested_json_is_rejected_as_invalid_json() -> None:
    """A RecursionError from json.loads must not kill the connection."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            # 20 000 levels is enough to overflow the C JSON decoder stack.
            ws.send_text("[" * 20000 + "]" * 20000)
            reply = json.loads(ws.receive_text())
            assert reply == {"type": "ERROR", "message": "invalid_json"}
            _join_room(ws)


def test_proposal_payload_must_be_dict() -> None:
    """A proposal payload that is not a dict must be rejected gracefully."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(
                _message(
                    "not-a-dict",
                    seq=1,
                    content_type="application/perun-proposal",
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "ERROR"
            # Socket is still usable.
            _join_room(ws)


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("sequence_number", "x"),
        ("sequence_number", []),
        ("timestamp", "x"),
        ("timestamp", []),
        ("thread_id", 123),
        ("id", 123),
    ],
)
def test_badly_typed_envelope_fields_return_error(field: str, bad_value: Any) -> None:
    """Fields that security or frame validation expect to be numeric/string must not crash the socket."""
    key = _seed_key()
    envelope = {
        "id": "msg_ok",
        "type": "MESSAGE",
        "room_id": "grp:test:room",
        "room_type": "GROUP",
        "sequence_number": 1,
        "timestamp": int(time.time() * 1000),
        "thread_id": "default",
        "content_type": "text/plain",
        "payload": "hello",
    }
    envelope[field] = bad_value
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(json.dumps(envelope))
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "ERROR"
            # Socket remains usable (re-joining the same room is enough proof).
            _join_room(ws)


@pytest.mark.parametrize("bad_sender", ["x", []])
def test_non_dict_sender_gets_error_and_socket_stays_open(bad_sender: Any) -> None:
    """A sender field that is not a dict must not crash the socket."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(
                json.dumps(
                    {
                        "id": "msg_bad_sender",
                        "type": "MESSAGE",
                        "room_id": "grp:test:room",
                        "room_type": "GROUP",
                        "sequence_number": 1,
                        "timestamp": int(time.time() * 1000),
                        "thread_id": "default",
                        "content_type": "text/plain",
                        "sender": bad_sender,
                        "payload": "hi",
                    }
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "ERROR"
            _join_room(ws)


def test_staged_tokenless_proposals_do_not_lock_room() -> None:
    """Tokenless proposals are staged, not broadcast, and must not burn credits."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers(key, runtime="claude")
        ) as ws:
            _join_room(ws, "pub:burst")
            for i in range(1, 31):
                ws.send_text(
                    _message(
                        {"proposal_id": f"p{i}"},
                        room_id="pub:burst",
                        room_type="PUBLIC",
                        content_type="application/perun-proposal",
                        seq=i,
                    )
                )
                reply = json.loads(ws.receive_text())
                assert reply["type"] == "PROPOSAL_STAGED"
                time.sleep(0.15)
            # A normal frame should still be accepted because no credit was consumed.
            ws.send_text(
                _message(
                    "still here",
                    room_id="pub:burst",
                    room_type="PUBLIC",
                    seq=31,
                    id="msg_ok",
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["id"] == "msg_ok"


def test_operator_spoof_does_not_disable_burst_lock() -> None:
    """A non-operator key cannot claim operator runtime via sender.runtime."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers(key, runtime="claude")
        ) as ws:
            _join_room(ws, "pub:spoof")
            replies: list[dict] = []
            for i in range(1, 31):
                ws.send_text(
                    _message(
                        f"frame {i}",
                        room_id="pub:spoof",
                        room_type="PUBLIC",
                        seq=i,
                        id=f"s{i}",
                        sender={"agent_id": "test-agent", "runtime": "operator"},
                    )
                )
                replies.append(json.loads(ws.receive_text()))
                time.sleep(0.15)
            assert any(
                r.get("type") == "ERROR"
                and r.get("message") == "thread_locked_pending_operator"
                for r in replies
            )


# --------------------------------------------------------------------------- #
# External execution handoff: scoped frame acceptance in managed group rooms.
# --------------------------------------------------------------------------- #
def _create_managed_room(room_id: str, managed_by: str, monkeypatch) -> str:
    """Create an API key and a managed group room; return the plaintext key."""
    from perunchat import config as config_module
    from perunchat import db as db_module

    db_file = str(gateway.keystore.db_path)
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)
    raw, _ = gateway.keystore.create(
        owner_id="owner-exec",
        agent_id=managed_by,
        supported_runtime="perun",
        scopes=["grp:test:*"],
    )
    client = TestClient(app)
    r = client.post(
        "/api/v1/rooms/group",
        json={"room_id": room_id, "members": []},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert r.status_code == 200, r.text
    return raw


def _execution_frame(
    room_id: str,
    seq: int,
    payload: dict,
    content_type: str = "application/perun-execution",
) -> dict:
    import time
    return {
        "id": f"exec_{seq}",
        "trace_id": "trc_exec",
        "thread_id": "default",
        "sequence_number": seq,
        "timestamp": int(time.time() * 1000),
        "room_type": "GROUP",
        "room_id": room_id,
        "sender": {"agent_id": "manager", "runtime": "perun"},
        "content_type": content_type,
        "turn_count": 0,
        "payload": payload,
    }


def _package_frame(
    room_id: str,
    seq: int,
    transfer_id: str,
    data: str,
    recipient_id: str | None = None,
) -> dict:
    import time
    frame = {
        "id": f"pkg_{seq}",
        "trace_id": "trc_pkg",
        "thread_id": "default",
        "sequence_number": seq + 100,
        "timestamp": int(time.time() * 1000),
        "room_type": "GROUP",
        "room_id": room_id,
        "sender": {"agent_id": "manager", "runtime": "perun"},
        "content_type": "application/perun-package",
        "turn_count": 0,
        "payload": {"transfer_id": transfer_id, "seq": seq, "total_chunks": 1, "data": data},
    }
    if recipient_id:
        frame["recipient_id"] = recipient_id
    return frame


def test_execution_frames_rejected_in_public_room() -> None:
    """Execution frames are only accepted in managed group rooms."""
    raw, _ = gateway.keystore.create(
        owner_id="owner-pub",
        agent_id="pub-agent",
        supported_runtime="perun",
        scopes=["pub:global:*"],
    )
    room_id = "pub:global:exec-reject"
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "pub-agent", "perun")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "PUBLIC"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(json.dumps(_execution_frame(room_id, 1, {"op": "execution_status"})))
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert err["message"] == "package_frames_project_rooms_only"


def test_package_frames_rejected_in_unmanaged_group_room() -> None:
    """A plain grp: room without a managed_by row rejects package frames."""
    raw, _ = gateway.keystore.create(
        owner_id="owner-grp",
        agent_id="grp-agent",
        supported_runtime="perun",
        scopes=["grp:test:*"],
    )
    room_id = "grp:test:unmanaged:room"
    gateway.rooms.add_member(room_id, "grp-agent")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "grp-agent", "perun")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            ws.send_text(json.dumps(_package_frame(room_id, 0, "trf_1", "Zg==")))
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert err["message"] == "package_frames_project_rooms_only"


def test_execution_result_from_non_owner_is_rejected(monkeypatch) -> None:
    """Only the managed-room owner may send execution-result frames."""
    room_id = "grp:test:exec:owner-only"
    _create_managed_room(room_id, "manager", monkeypatch)
    raw, _ = gateway.keystore.create(
        owner_id="owner-frambo",
        agent_id="frambo",
        supported_runtime="custom",
        scopes=["grp:test:*"],
    )
    gateway.rooms.add_member(room_id, "frambo")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "frambo", "custom")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED
            ws.send_text(
                json.dumps(
                    _execution_frame(
                        room_id,
                        1,
                        {"op": "claim_execution", "proposal_id": "prop_x"},
                        content_type="application/perun-execution-result",
                    )
                )
            )
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert err["message"] == "execution_result_owner_only"


def test_package_frame_requires_recipient(monkeypatch) -> None:
    """Package chunks must be directed to the room owner, never broadcast."""
    room_id = "grp:test:exec:recipient"
    _create_managed_room(room_id, "manager", monkeypatch)
    raw, _ = gateway.keystore.create(
        owner_id="owner-frambo",
        agent_id="frambo",
        supported_runtime="custom",
        scopes=["grp:test:*"],
    )
    gateway.rooms.add_member(room_id, "frambo")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "frambo", "custom")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED
            ws.send_text(json.dumps(_package_frame(room_id, 1, "trf_x", "Zg==")))
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert err["message"] == "package_recipient_required"


def test_package_frame_must_target_owner(monkeypatch) -> None:
    """A claimant cannot direct file chunks to another room member."""
    room_id = "grp:test:exec:target"
    _create_managed_room(room_id, "manager", monkeypatch)
    raw, _ = gateway.keystore.create(
        owner_id="owner-frambo",
        agent_id="frambo",
        supported_runtime="custom",
        scopes=["grp:test:*"],
    )
    gateway.rooms.add_member(room_id, "frambo")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "frambo", "custom")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED
            ws.send_text(
                json.dumps(
                    _package_frame(
                        room_id, 1, "trf_x", "Zg==", recipient_id="someone-else"
                    )
                )
            )
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert err["message"] == "package_recipient_must_be_owner"


def test_package_frame_to_owner_is_accepted(monkeypatch) -> None:
    """Chunks directed to the managed-room owner are accepted."""
    room_id = "grp:test:exec:ok"
    _create_managed_room(room_id, "manager", monkeypatch)
    raw, _ = gateway.keystore.create(
        owner_id="owner-frambo",
        agent_id="frambo",
        supported_runtime="custom",
        scopes=["grp:test:*"],
    )
    gateway.rooms.add_member(room_id, "frambo")
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(raw, "frambo", "custom")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED
            ws.send_text(
                json.dumps(
                    _package_frame(
                        room_id, 1, "trf_x", "Zg==", recipient_id="manager"
                    )
                )
            )
            # No error: send a normal chat frame and confirm the connection
            # is still healthy.
            ws.send_text(_message("still here", room_id=room_id, seq=200))
            echoed = json.loads(ws.receive_text())
            assert echoed["payload"] == "still here"


def test_directed_frame_only_reaches_recipient(monkeypatch) -> None:
    """Directed frames never reach other members and are not replayed."""
    room = _room(monkeypatch, n=0)

    async def scenario() -> tuple[FakeWS, FakeWS, FakeWS]:
        alice, bob = FakeWS(), FakeWS()
        await room.join(alice, "alice")
        await room.join(bob, "bob")
        await room.replay_history(alice, "alice")
        await room.replay_history(bob, "bob")
        frame = _frame(1, content_type="application/perun-execution-result")
        frame["recipient_id"] = "bob"
        await room.broadcast(alice, frame)
        late = FakeWS()
        await room.join(late, "late")
        await room.replay_history(late, "late")
        return alice, bob, late

    alice, bob, late = asyncio.run(scenario())
    assert [f["id"] for f in bob.sent] == ["m1"]
    assert alice.sent == []
    assert late.sent == []


def test_room_managed_by_uses_short_lived_cache(tmp_path, monkeypatch) -> None:
    """room_managed_by caches the result and is invalidated by lifecycle helpers."""
    from perunchat import config as config_module
    from perunchat import db as db_module
    from perunchat.rooms import (
        group_room_id,
        invalidate_managed_room_cache,
        room_managed_by,
    )

    db_file = str(tmp_path / "rooms.db")
    monkeypatch.setattr(config_module, "db_path", lambda: db_file)
    db_module.init_db(db_file)
    room_id = group_room_id("acme", "proj", "cache")

    # No row -> None, and the cache stores the miss.
    assert room_managed_by(room_id) is None
    assert room_managed_by(room_id) is None

    # Create the room and invalidate the cache.
    with db_module.db(db_file) as conn:
        conn.execute(
            "INSERT INTO rooms (room_id, managed_by, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (room_id, "manager-bot", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    # Without invalidation the stale cached None is returned.
    assert room_managed_by(room_id) is None
    invalidate_managed_room_cache(room_id)
    assert room_managed_by(room_id) == "manager-bot"


def test_package_frames_accepted_in_managed_group_room(monkeypatch) -> None:
    """Managed group rooms allow larger package payloads and skip DLP/turn counters."""
    room_id = "grp:test:managed:room"
    key = _create_managed_room(room_id, "manager", monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/v1/chat/ws", headers=_headers_optional_runtime(key, "manager", "perun")
        ) as ws:
            ws.send_text(
                json.dumps({"type": "ROOM_JOIN", "room_id": room_id, "room_type": "GROUP"})
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            # A control frame is echoed normally (DLP still applies).
            ws.send_text(
                json.dumps(_execution_frame(room_id, 1, {"op": "execution_status", "proposal_id": "p"}))
            )
            echo = json.loads(ws.receive_text())
            assert echo["content_type"] == "application/perun-execution"

            # A package frame is accepted even though it is > normal 8 KiB limit.
            big_data = "a" * (10 * 1024)
            ws.send_text(
                json.dumps(
                    _package_frame(
                        room_id, 2, "trf_2", big_data, recipient_id="manager"
                    )
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["content_type"] == "application/perun-package"
            assert echo["payload"]["data"] == big_data

            # Package frames bypass DLP; execution frames are still scanned.
            secret = "prn_live_abcdef1234567890secretvalue"
            ws.send_text(
                json.dumps(_execution_frame(room_id, 103, {"op": "note", "text": secret}))
            )
            echo = json.loads(ws.receive_text())
            assert echo["content_type"] == "application/perun-execution"
            assert "[REDACTED_SECRET]" in echo["payload"]["text"]
            assert secret not in echo["payload"]["text"]

            ws.send_text(
                json.dumps(
                    _package_frame(
                        room_id, 104, "trf_3", secret, recipient_id="manager"
                    )
                )
            )
            echo = json.loads(ws.receive_text())
            assert echo["content_type"] == "application/perun-package"
            assert echo["payload"]["data"] == secret

            # Package frames are not stored in history.
            assert not any(
                frame.get("content_type") == "application/perun-package"
                for _, frame in gateway._rooms[room_id]._history
            )


# --------------------------------------------------------------------------- #
# Two-phase proposal edge cases.
# --------------------------------------------------------------------------- #
def _stage_proposal(
    client: TestClient,
    key: str,
    proposal_id: str,
    details: dict,
    sequence_number: int = 1,
    expect_conflict: bool = False,
) -> None:
    import uuid

    now_ms = int(time.time() * 1000)
    msg_id = f"msg_stage_{uuid.uuid4().hex[:8]}"
    with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
        ws.send_text(
            json.dumps(
                {
                    "id": f"{msg_id}_join",
                    "type": "ROOM_JOIN",
                    "room_id": "grp:test:room",
                    "room_type": "GROUP",
                    "sequence_number": sequence_number,
                    "timestamp": now_ms,
                }
            )
        )
        json.loads(ws.receive_text())  # ROOM_JOINED
        ws.send_text(
            json.dumps(
                {
                    "id": msg_id,
                    "trace_id": "trc_stage",
                    "thread_id": "default",
                    "sequence_number": sequence_number + 1,
                    "timestamp": now_ms,
                    "room_type": "GROUP",
                    "room_id": "grp:test:room",
                    "sender": {"agent_id": "test-agent", "runtime": "custom"},
                    "content_type": "application/perun-proposal",
                    "turn_count": 0,
                    "payload": {
                        "proposal_id": proposal_id,
                        "proposal_type": "PERUN_ACTION_PROPOSAL",
                        "risk_tier": "TIER_2_LOW_IMPACT",
                        "action_domain": "shell",
                        "description": "Run echo hello",
                        "execution_details": details,
                        "provenance": {"instigator": "test-agent", "justification": "demo"},
                    },
                }
            )
        )
        ack = json.loads(ws.receive_text())
        if expect_conflict:
            assert ack["type"] == "ERROR"
            assert ack["message"] == "proposal_id_conflict"
        else:
            assert ack["type"] == "PROPOSAL_STAGED"


def test_duplicate_proposal_id_staging_is_rejected() -> None:
    """Staging a proposal_id that is already pending is rejected to prevent swaps."""
    key = _seed_key()
    with TestClient(app) as client:
        _stage_proposal(client, key, "prop_dup", {"command": "echo first"}, sequence_number=1)
        _stage_proposal(
            client, key, "prop_dup", {"command": "echo second"}, sequence_number=3,
            expect_conflict=True,
        )
        staged = api_module.proposal_store.get("prop_dup")
        assert staged is not None
        assert staged["payload"]["execution_details"]["command"] == "echo first"


def test_proposal_store_full_is_rejected_without_eviction(monkeypatch) -> None:
    """A full staging store rejects new proposals instead of dropping an old one."""
    key = _seed_key()
    monkeypatch.setattr(api_module.ProposalStore, "_MAX_PENDING_PROPOSALS", 1)
    with TestClient(app) as client:
        _stage_proposal(client, key, "prop_first", {"command": "echo 1"}, sequence_number=1)

        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(
                _message(
                    {
                        "proposal_id": "prop_second",
                        "proposal_type": "PERUN_ACTION_PROPOSAL",
                        "risk_tier": "TIER_2_LOW_IMPACT",
                        "action_domain": "shell",
                        "description": "Run echo 2",
                        "execution_details": {"command": "echo 2"},
                        "provenance": {"instigator": "test-agent", "justification": "demo"},
                    },
                    content_type="application/perun-proposal",
                    seq=3,
                )
            )
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "ERROR"
            assert reply["message"] == "proposal_store_full"

        # The first proposal must not have been evicted.
        assert api_module.proposal_store.get("prop_first") is not None
        assert api_module.proposal_store.get("prop_second") is None


def test_old_proposal_remains_approvable() -> None:
    """Proposals do not expire; an old staged proposal can still be approved."""
    key = _seed_key()
    proposal_id = "prop_old"
    with TestClient(app) as client:
        _stage_proposal(client, key, proposal_id, {"command": "echo hello"}, sequence_number=1)
        # Backdate the proposal by a large amount.
        with api_module.proposal_store._with_conn() as conn:
            conn.execute(
                "UPDATE pending_proposals SET created_at = ? WHERE proposal_id = ?",
                (int(time.time()) - 10_000_000, proposal_id),
            )
            conn.commit()

        resp = client.post(
            f"/api/v1/proposals/{proposal_id}/approve",
            json={"execution_details": {"command": "echo hello"}},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert "approval_token" in resp.json()


def test_approval_token_nonce_replay_is_rejected() -> None:
    """Resubmitting a proposal with a previously consumed token fails."""
    import uuid

    agent_key = _seed_key()
    operator_raw, _ = gateway.keystore.create(
        owner_id="owner-op",
        agent_id="operator-agent",
        supported_runtime="custom",
        scopes=["grp:test:room"],
    )
    now_ms = int(time.time() * 1000)
    with TestClient(app) as client:
        _stage_proposal(client, agent_key, "prop_replay", {"command": "echo hello"}, sequence_number=1)

        approve_resp = client.post(
            "/api/v1/proposals/prop_replay/approve",
            json={"execution_details": {"command": "echo hello"}},
            headers={"Authorization": f"Bearer {operator_raw}"},
        )
        assert approve_resp.status_code == 200
        token = approve_resp.json()["approval_token"]

        # Both resubmits use the same WebSocket so history replay does not
        # interfere with the assertions.
        with client.websocket_connect("/v1/chat/ws", headers=_headers(agent_key)) as ws:
            ws.send_text(
                json.dumps(
                    {
                        "id": f"join_{uuid.uuid4().hex[:8]}",
                        "type": "ROOM_JOIN",
                        "room_id": "grp:test:room",
                        "room_type": "GROUP",
                        "sequence_number": 10,
                        "timestamp": now_ms,
                    }
                )
            )
            json.loads(ws.receive_text())  # ROOM_JOINED

            # First resubmit broadcasts the proposal.
            ws.send_text(
                json.dumps(
                    {
                        "id": f"first_{uuid.uuid4().hex[:8]}",
                        "trace_id": "trc_first",
                        "thread_id": "default",
                        "sequence_number": 11,
                        "timestamp": now_ms,
                        "room_type": "GROUP",
                        "room_id": "grp:test:room",
                        "sender": {"agent_id": "test-agent", "runtime": "custom"},
                        "content_type": "application/perun-proposal",
                        "turn_count": 0,
                        "payload": {
                            "proposal_id": "prop_replay",
                            "proposal_type": "PERUN_ACTION_PROPOSAL",
                            "risk_tier": "TIER_2_LOW_IMPACT",
                            "action_domain": "shell",
                            "description": "Run echo hello",
                            "execution_details": {"command": "echo hello"},
                            "provenance": {"instigator": "test-agent", "justification": "demo"},
                            "approval_token": token["token"],
                            "approval_expiry_epoch": token["expiry_epoch"],
                            "approval_nonce": token["nonce"],
                        },
                    }
                )
            )
            broadcast = json.loads(ws.receive_text())
            assert broadcast["content_type"] == "application/perun-proposal"

            # Second resubmit with the same token is rejected as replay.
            ws.send_text(
                json.dumps(
                    {
                        "id": f"second_{uuid.uuid4().hex[:8]}",
                        "trace_id": "trc_second",
                        "thread_id": "default",
                        "sequence_number": 12,
                        "timestamp": now_ms + 1,
                        "room_type": "GROUP",
                        "room_id": "grp:test:room",
                        "sender": {"agent_id": "test-agent", "runtime": "custom"},
                        "content_type": "application/perun-proposal",
                        "turn_count": 0,
                        "payload": {
                            "proposal_id": "prop_replay",
                            "proposal_type": "PERUN_ACTION_PROPOSAL",
                            "risk_tier": "TIER_2_LOW_IMPACT",
                            "action_domain": "shell",
                            "description": "Run echo hello",
                            "execution_details": {"command": "echo hello"},
                            "provenance": {"instigator": "test-agent", "justification": "demo"},
                            "approval_token": token["token"],
                            "approval_expiry_epoch": token["expiry_epoch"],
                            "approval_nonce": token["nonce"],
                        },
                    }
                )
            )
            err = json.loads(ws.receive_text())
            assert err["type"] == "ERROR"
            assert "nonce_replay" in err["message"]
            assert api_module.proposal_store.get("prop_replay") is None


@pytest.mark.parametrize(
    "bad_payload",
    [
        {},
        {"proposal_id": ""},
        {"proposal_id": None},
        {"title": "x"},
        [],
        "",
        None,
    ],
)
def test_proposal_without_id_is_not_broadcast(bad_payload: Any) -> None:
    """A proposal frame with no usable proposal_id must be rejected, not broadcast."""
    key_a = _seed_key(agent_id="agent-a")
    key_b = _seed_key(agent_id="agent-b")
    with TestClient(app) as client:
        with (
            client.websocket_connect(
                "/v1/chat/ws", headers=_headers(key_a, agent_id="agent-a")
            ) as a,
            client.websocket_connect(
                "/v1/chat/ws", headers=_headers(key_b, agent_id="agent-b")
            ) as b,
        ):
            _join_room(a, "pub:np")
            _join_room(b, "pub:np")
            a.send_text(
                _message(
                    bad_payload,
                    room_id="pub:np",
                    room_type="PUBLIC",
                    content_type="application/perun-proposal",
                    seq=1,
                    id="np1",
                )
            )
            reply = json.loads(a.receive_text())
            assert reply["type"] == "ERROR"

            a.send_text(
                _message(
                    "follow-up",
                    room_id="pub:np",
                    room_type="PUBLIC",
                    seq=2,
                    id="np2",
                )
            )
            echo_a = json.loads(a.receive_text())
            echo_b = json.loads(b.receive_text())
            assert echo_a["id"] == "np2"
            assert echo_b["id"] == "np2"


def test_client_cannot_inject_internal_state_keys() -> None:
    """Inbound frames must not carry `_approval_validated` or `_staged_at`."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            ws.send_text(
                _message(
                    "hello",
                    seq=1,
                    id="inject1",
                    _approval_validated=True,
                    _staged_at=123,
                )
            )
            echo = json.loads(ws.receive_text())
            assert "_approval_validated" not in echo
            assert "_staged_at" not in echo


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("approval_token", 123),
        ("approval_token", ["x"]),
        ("approval_token", {"a": 1}),
        ("approval_token", "é"),
        ("approval_nonce", ["n"]),
        ("approval_nonce", {"n": 1}),
        ("approval_expiry_epoch", "x"),
        ("approval_expiry_epoch", None),
        ("approval_expiry_epoch", True),
        ("approval_expiry_epoch", float("nan")),
        ("execution_details", "not-a-dict"),
    ],
)
def test_malformed_approval_fields_do_not_crash_socket(
    field: str, bad_value: Any
) -> None:
    """Malformed HITL token fields must return ERROR, not disconnect."""
    key = _seed_key()
    with TestClient(app) as client:
        with client.websocket_connect("/v1/chat/ws", headers=_headers(key)) as ws:
            _join_room(ws)
            envelope = {
                "id": "bad_tok",
                "type": "MESSAGE",
                "room_id": "grp:test:room",
                "room_type": "GROUP",
                "sequence_number": 1,
                "timestamp": int(time.time() * 1000),
                "thread_id": "default",
                "sender": {"agent_id": "test-agent", "runtime": "custom"},
                "content_type": "application/perun-proposal",
                "turn_count": 0,
                "payload": {
                    "proposal_id": "prop_bad_tok",
                    "proposal_type": "PERUN_ACTION_PROPOSAL",
                    "risk_tier": "TIER_2_LOW_IMPACT",
                    "action_domain": "shell",
                    "description": "x",
                    "execution_details": {"command": "echo x"},
                    "provenance": {"instigator": "test-agent", "justification": "demo"},
                    "approval_token": "dummy-token",
                    "approval_expiry_epoch": int(time.time()) + 300,
                    "approval_nonce": "nonce",
                },
            }
            envelope["payload"][field] = bad_value
            ws.send_text(json.dumps(envelope))
            reply = json.loads(ws.receive_text())
            assert reply["type"] == "ERROR"
            # Socket is still usable.
            _join_room(ws)
