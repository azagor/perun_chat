"""Tests for the ZeroClaw micro-client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from perunchat.zeroclaw_client import ZeroClawClient, connect_zeroclaw


def test_connect_zeroclaw_joins_room() -> None:
    mock_ws = MagicMock()
    mock_create = patch(
        "perunchat.zeroclaw_client.websocket.create_connection",
        return_value=mock_ws,
    )
    with mock_create as create_mock:
        client = connect_zeroclaw("prn_live_key", "zc-01", "grp:test:room")

    create_mock.assert_called_once()
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["type"] == "ROOM_JOIN"
    assert sent["room_id"] == "grp:test:room"
    assert sent["room_type"] == "GROUP"
    client.close()


def test_send_text() -> None:
    mock_ws = MagicMock()
    client = ZeroClawClient("prn_live_key", "zc-01")
    client.ws = mock_ws

    client.send_text("grp:test:room", "GROUP", "hello from edge")
    sent = json.loads(mock_ws.send.call_args[0][0])
    assert sent["content_type"] == "text/plain"
    assert sent["payload"] == "hello from edge"
    assert sent["room_id"] == "grp:test:room"


def test_poll_invalid_json() -> None:
    mock_ws = MagicMock()
    mock_ws.recv.return_value = "not-json"
    client = ZeroClawClient("prn_live_key", "zc-01")
    client.ws = mock_ws

    frame = client.poll()
    assert frame["type"] == "ERROR"
    assert frame["message"] == "invalid_json"
