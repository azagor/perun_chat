"""Tests for the MCP bridge skeleton."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from unittest.mock import AsyncMock, Mock, patch

import pytest

from perunchat.mcp_bridge import PerunChatMCPServer, serve_mcp


@pytest.fixture
def server() -> PerunChatMCPServer:
    return PerunChatMCPServer("ws://localhost/ws", "key", "agent-1")


@pytest.mark.asyncio
async def test_list_resources(server: PerunChatMCPServer) -> None:
    resources = server.list_resources()
    assert any("perunchat://rooms/{room_id}" == r["uri"] for r in resources)


@pytest.mark.asyncio
async def test_list_tools(server: PerunChatMCPServer) -> None:
    tools = server.list_tools()
    names = {t["name"] for t in tools}
    assert "perunchat_send_message" in names
    assert "perunchat_propose_action" in names
    assert "perunchat_claim_execution" in names
    assert "perunchat_request_download_package" in names
    assert "perunchat_upload_package" in names
    assert "perunchat_write_target_file" in names
    assert "perunchat_execution_status" in names
    assert "perunchat_revoke_execution_claim" in names


def _install_client(server: PerunChatMCPServer, send_frame) -> AsyncMock:
    from perunchat.client import RiskTier, RoomType

    client = AsyncMock()
    client.ws = AsyncMock()
    client.ws.close_code = None
    client.on_message = Mock()
    client.send_frame = AsyncMock(side_effect=send_frame)
    client.join_room = AsyncMock()
    client._joined_rooms = set()
    server._client = client
    server._RoomType = RoomType
    server._RiskTier = RiskTier
    return client


def _zip_bytes(path: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(path, body)
    return buf.getvalue()


def _push_control(
    server: PerunChatMCPServer, content_type: str, payload: dict
) -> None:
    server._control_seq += 1
    server._control_frames.append(
        {
            "content_type": content_type,
            "payload": payload,
            "_mcp_seq": server._control_seq,
        }
    )


@pytest.mark.asyncio
async def test_claim_execution_waits_for_result(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload):
        assert content_type == "application/perun-execution"
        _push_control(
            server,
            "application/perun-execution-result",
            {
                "op": "claim_execution",
                "proposal_id": payload["proposal_id"],
                "ok": True,
                "proposal": {"id": payload["proposal_id"], "status": "claimed"},
            },
        )
        return {}

    _install_client(server, send)
    result = await server.call_tool(
        "perunchat_claim_execution",
        {"room_id": "grp:perun:demo:chat", "proposal_id": "prop_test"},
    )
    assert result["isError"] is False
    assert '"status": "claimed"' in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_download_package_writes_verified_zip(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    digest = hashlib.sha256(archive).hexdigest()
    transfer_id = "trf_download"
    server._package_chunks[transfer_id] = {0: archive}
    server._package_bytes[transfer_id] = len(archive)

    async def send(room_id, room_type, content_type, payload):
        assert payload["op"] == "request_download_package"
        _push_control(
            server,
            "application/perun-execution",
            {
                "op": "package_ready",
                "proposal_id": "prop_test",
                "transfer_id": transfer_id,
                "sha256": digest,
                "total_size": len(archive),
                "total_chunks": 1,
            },
        )
        return {}

    _install_client(server, send)
    destination = tmp_path / "package.zip"
    result = await server.call_tool(
        "perunchat_request_download_package",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "destination": str(destination),
        },
    )
    assert result["isError"] is False
    assert destination.read_bytes() == archive
    payload = json.loads(result["content"][0]["text"])
    assert payload["sha256"] == digest
    assert transfer_id not in server._package_chunks


@pytest.mark.asyncio
async def test_upload_package_sends_begin_chunks_end(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(archive)
    frames: list[dict] = []

    async def send(room_id, room_type, content_type, payload):
        frames.append({"content_type": content_type, "payload": payload})
        if payload.get("op") == "package_begin":
            _push_control(
                server,
                "application/perun-execution-result",
                {
                    "op": "package_begin",
                    "proposal_id": payload["proposal_id"],
                    "transfer_id": payload["transfer_id"],
                    "ok": True,
                },
            )
        elif payload.get("op") == "package_end":
            _push_control(
                server,
                "application/perun-execution-result",
                {
                    "op": "package_accepted",
                    "proposal_id": "prop_test",
                    "transfer_id": payload["transfer_id"],
                    "ok": True,
                },
            )
        return {}

    _install_client(server, send)
    result = await server.call_tool(
        "perunchat_upload_package",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "zip_path": str(package_path),
            "chunk_bytes": 16384,
        },
    )
    assert result["isError"] is False
    assert frames[0]["payload"]["op"] == "package_begin"
    assert frames[0]["payload"]["sha256"] == hashlib.sha256(archive).hexdigest()
    assert frames[-1]["payload"]["op"] == "package_end"
    chunks = [
        frame
        for frame in frames
        if frame["content_type"] == "application/perun-package"
    ]
    assert len(chunks) == 1
    assert base64.b64decode(chunks[0]["payload"]["data"]) == archive


@pytest.mark.asyncio
async def test_write_target_file_builds_single_file_zip(
    server: PerunChatMCPServer,
) -> None:
    captured: dict = {}

    async def fake_upload(client, room_id, room_type, proposal_id, archive, chunk_bytes):
        captured["archive"] = archive
        return {"ok": True, "op": "package_accepted", "proposal_id": proposal_id}

    server._upload_archive = AsyncMock(side_effect=fake_upload)
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_write_target_file",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "path": "CANARY7.md",
            "content": "line1\nline2\nline3\n",
        },
    )
    assert result["isError"] is False
    with zipfile.ZipFile(io.BytesIO(captured["archive"])) as archive:
        assert archive.read("CANARY7.md") == b"line1\nline2\nline3\n"


@pytest.mark.asyncio
async def test_write_target_file_rejects_absolute_path(
    server: PerunChatMCPServer,
) -> None:
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_write_target_file",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "path": "/etc/passwd",
            "content": "nope",
        },
    )
    assert result["isError"] is True
    assert "project-relative" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_unsolicited_package_chunks_are_ignored(
    server: PerunChatMCPServer,
) -> None:
    chunk = {
        "content_type": "application/perun-package",
        "payload": {
            "transfer_id": "trf_unsolicited",
            "seq": 0,
            "data": base64.b64encode(b"junk").decode("ascii"),
        },
    }
    server._buffer_package_frame(chunk)
    assert "trf_unsolicited" not in server._package_chunks

    server._on_frame(
        {
            "content_type": "application/perun-execution",
            "payload": {
                "op": "package_ready",
                "proposal_id": "prop_test",
                "transfer_id": "trf_unsolicited",
                "sha256": hashlib.sha256(b"junk").hexdigest(),
                "total_size": 4,
                "total_chunks": 1,
            },
        }
    )
    server._buffer_package_frame(chunk)
    assert server._package_chunks["trf_unsolicited"][0] == b"junk"


@pytest.mark.asyncio
async def test_handoff_paths_are_confined_to_handoff_dir(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    handoff = tmp_path / "handoff"
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(handoff))
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"not a package")
    _install_client(server, AsyncMock(return_value={}))
    upload = await server.call_tool(
        "perunchat_upload_package",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "zip_path": str(outside),
        },
    )
    assert upload["isError"] is True
    assert "handoff path must be inside" in upload["content"][0]["text"]

    download = await server.call_tool(
        "perunchat_request_download_package",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "destination": str(outside.with_name("download.zip")),
        },
    )
    assert download["isError"] is True
    assert "handoff path must be inside" in download["content"][0]["text"]


@pytest.mark.asyncio
async def test_execution_status_returns_payload(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload):
        _push_control(
            server,
            "application/perun-execution-result",
            {
                "op": "execution_status",
                "proposal_id": payload["proposal_id"],
                "ok": True,
                "proposal": {"id": payload["proposal_id"], "status": "approved"},
            },
        )
        return {}

    _install_client(server, send)
    result = await server.call_tool(
        "perunchat_execution_status",
        {"room_id": "grp:perun:demo:chat", "proposal_id": "prop_test"},
    )
    assert result["isError"] is False
    assert '"status": "approved"' in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_call_tool_send_message(server: PerunChatMCPServer) -> None:
    mock_client = AsyncMock()
    mock_client.ws = AsyncMock()
    mock_client.ws.close_code = None
    mock_client.send_text = AsyncMock(return_value={"id": "msg_1"})
    mock_client.on_message = Mock()
    with patch("perunchat.client.PerunChatClient", return_value=mock_client):
        result = await server.call_tool(
            "perunchat_send_message",
            {"room_id": "grp:test:room", "room_type": "GROUP", "text": "hi"},
        )
    assert "content" in result
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "text"
    assert result["isError"] is False
    mock_client.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_call_tool_unknown(server: PerunChatMCPServer) -> None:
    result = await server.call_tool("unknown_tool", {})
    assert result["isError"] is True


@pytest.mark.asyncio
async def test_read_resource_starts_client(server: PerunChatMCPServer) -> None:
    mock_client = AsyncMock()
    mock_client.ws = AsyncMock()
    mock_client.ws.close_code = None
    mock_client.on_message = Mock()
    with patch("perunchat.client.PerunChatClient", return_value=mock_client):
        text = await server.read_resource("perunchat://rooms/grp:test:room")
    assert text == "[]"


def test_serve_mcp_stub_does_not_crash() -> None:
    # Without the official `mcp` package installed this should print and return.
    with patch("perunchat.mcp_bridge._HAS_MCP", False):
        with patch("builtins.print") as print_mock:
            serve_mcp("ws://localhost/ws", "key", "agent-1", "stdio")
    assert any("perunchat_send_message" in str(c) for c in print_mock.call_args_list)
