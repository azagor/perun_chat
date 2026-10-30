"""Tests for the MCP bridge skeleton."""

from __future__ import annotations

import asyncio
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
    server._on_frame(
        {
            "content_type": content_type,
            "payload": payload,
            "room_id": "grp:perun:demo:chat",
            "sender": {"agent_id": "peruntheoverseer", "runtime": "perun"},
        }
    )


@pytest.mark.asyncio
async def test_claim_execution_waits_for_result(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload, **kwargs):
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
    assert server._perun_agent_ids["grp:perun:demo:chat"] == "peruntheoverseer"


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

    async def send(room_id, room_type, content_type, payload, **kwargs):
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

    async def send(room_id, room_type, content_type, payload, **kwargs):
        frames.append(
            {
                "content_type": content_type,
                "payload": payload,
                "recipient_id": kwargs.get("recipient_id"),
            }
        )
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
    server._perun_agent_ids["grp:perun:demo:chat"] = "peruntheoverseer"
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
    assert all(frame["recipient_id"] == "peruntheoverseer" for frame in frames)
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
async def test_upload_success_without_ok_is_not_error(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    """Perun <=0.8.47 sent package_accepted without ``ok``; still a success."""
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(archive)

    async def send(room_id, room_type, content_type, payload, **kwargs):
        if payload.get("op") == "package_begin":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_begin",
                "proposal_id": payload["proposal_id"],
                "transfer_id": payload["transfer_id"],
                "ok": True,
            })
        elif payload.get("op") == "package_end":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_accepted",
                "proposal_id": "prop_test",
                "transfer_id": payload["transfer_id"],
            })
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
    assert '"ok": true' in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_upload_failure_frame_is_surfaced(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(archive)

    async def send(room_id, room_type, content_type, payload, **kwargs):
        if payload.get("op") == "package_begin":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_begin",
                "proposal_id": payload["proposal_id"],
                "transfer_id": payload["transfer_id"],
                "ok": True,
            })
        elif payload.get("op") == "package_end":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_end",
                "ok": False,
                "transfer_id": payload["transfer_id"],
                "error": "sha256 mismatch",
            })
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
    assert result["isError"] is True
    assert "sha256 mismatch" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_upload_ignores_rejection_for_another_transfer(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(archive)

    async def send(room_id, room_type, content_type, payload, **kwargs):
        if payload.get("op") == "package_begin":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_begin",
                "proposal_id": payload["proposal_id"],
                "transfer_id": payload["transfer_id"],
                "ok": True,
            })
        elif payload.get("op") == "package_end":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_rejected",
                "transfer_id": "trf_other",
                "error": "wrong transfer",
            })
            _push_control(server, "application/perun-execution-result", {
                "op": "package_rejected",
                "transfer_id": payload["transfer_id"],
                "error": "right transfer",
            })
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
    assert result["isError"] is True
    assert "right transfer" in result["content"][0]["text"]
    assert "wrong transfer" not in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_upload_accepts_lenient_rejection_without_transfer_id(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(archive)

    async def send(room_id, room_type, content_type, payload, **kwargs):
        if payload.get("op") == "package_begin":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_begin",
                "proposal_id": payload["proposal_id"],
                "transfer_id": payload["transfer_id"],
                "ok": True,
            })
        elif payload.get("op") == "package_end":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_rejected",
                "proposal_id": "prop_test",
                "error": "lenient rejection",
            })
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
    assert result["isError"] is True
    assert "lenient rejection" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_unrelated_rejection_is_ignored(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(archive)

    async def send(room_id, room_type, content_type, payload, **kwargs):
        if payload.get("op") == "package_begin":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_begin",
                "proposal_id": payload["proposal_id"],
                "transfer_id": payload["transfer_id"],
                "ok": True,
            })
        elif payload.get("op") == "package_end":
            _push_control(server, "application/perun-execution-result", {
                "op": "package_rejected",
                "error": "unrelated",
            })
        return {}

    server._PACKAGE_RESULT_TIMEOUT = 0.1
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
    assert result["isError"] is True
    assert "timed out" in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,expected",
    [
        (
            "perunchat_write_target_file",
            {"proposal_id": 123, "path": "README.md", "content": "x"},
            "proposal_id must be a string",
        ),
        (
            "perunchat_write_target_file",
            {"proposal_id": "p", "path": {"a": 1}, "content": "x"},
            "path must be a string",
        ),
        (
            "perunchat_upload_package",
            {"proposal_id": "p", "zip_path": 5},
            "zip_path must be a string",
        ),
        (
            "perunchat_execution_status",
            {"proposal_id": ["p"]},
            "proposal_id must be a string",
        ),
    ],
)
async def test_non_string_handoff_arguments_are_rejected(
    server: PerunChatMCPServer, tool: str, args: dict, expected: str
) -> None:
    server._upload_archive = AsyncMock()
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        tool, {"room_id": "grp:perun:demo:chat", **args}
    )
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_proposal_lock_is_released_on_cancellation(
    server: PerunChatMCPServer,
) -> None:
    entered = asyncio.Event()

    async def blocking_upload(client, room_id, room_type, proposal_id, archive, chunk_bytes):
        entered.set()
        await asyncio.sleep(30)

    server._upload_archive = blocking_upload
    _install_client(server, AsyncMock(return_value={}))
    args = {
        "room_id": "grp:perun:demo:chat",
        "proposal_id": "prop_cancel",
        "path": "README.md",
        "content": "x",
    }
    task = asyncio.create_task(server.call_tool("perunchat_write_target_file", args))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    server._upload_archive = AsyncMock(
        return_value={"ok": True, "op": "package_accepted", "transfer_id": "trf"}
    )
    result = await asyncio.wait_for(
        server.call_tool("perunchat_write_target_file", args), timeout=2
    )
    assert result["isError"] is False


@pytest.mark.asyncio
async def test_same_proposal_handoffs_are_serialized(
    server: PerunChatMCPServer,
) -> None:
    active = 0
    peak = 0

    async def fake_upload(client, room_id, room_type, proposal_id, archive, chunk_bytes):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"ok": True, "op": "package_accepted", "transfer_id": "trf"}

    server._upload_archive = fake_upload
    _install_client(server, AsyncMock(return_value={}))

    async def write(proposal_id: str):
        return await server.call_tool(
            "perunchat_write_target_file",
            {
                "room_id": "grp:perun:demo:chat",
                "proposal_id": proposal_id,
                "path": "README.md",
                "content": "x",
            },
        )

    results = await asyncio.gather(write("prop_a"), write("prop_a"))
    assert all(r["isError"] is False for r in results)
    assert peak == 1


@pytest.mark.asyncio
async def test_different_proposals_run_concurrently(
    server: PerunChatMCPServer,
) -> None:
    active = 0
    peak = 0

    async def fake_upload(client, room_id, room_type, proposal_id, archive, chunk_bytes):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {"ok": True, "op": "package_accepted", "transfer_id": "trf"}

    server._upload_archive = fake_upload
    _install_client(server, AsyncMock(return_value={}))

    async def write(proposal_id: str):
        return await server.call_tool(
            "perunchat_write_target_file",
            {
                "room_id": "grp:perun:demo:chat",
                "proposal_id": proposal_id,
                "path": "README.md",
                "content": "x",
            },
        )

    results = await asyncio.gather(write("prop_a"), write("prop_b"))
    assert all(r["isError"] is False for r in results)
    assert peak == 2


@pytest.mark.asyncio
async def test_claim_and_revoke_are_serialized(server: PerunChatMCPServer) -> None:
    active = 0
    peak = 0

    async def fake_send(client, room_id, room_type, payload):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return {}

    async def fake_wait(op, proposal_id, timeout=30.0, since=0, transfer_id="", room_id=""):
        return {"op": op, "ok": True, "proposal_id": proposal_id}

    server._send_execution = fake_send
    server._wait_for_result = fake_wait
    _install_client(server, AsyncMock(return_value={}))

    results = await asyncio.gather(
        server.call_tool(
            "perunchat_claim_execution",
            {"room_id": "grp:perun:demo:chat", "proposal_id": "prop_a"},
        ),
        server.call_tool(
            "perunchat_revoke_execution_claim",
            {"room_id": "grp:perun:demo:chat", "proposal_id": "prop_a"},
        ),
    )
    assert all(r["isError"] is False for r in results)
    assert peak == 1


@pytest.mark.asyncio
async def test_proposal_locks_are_keyed_per_proposal(
    server: PerunChatMCPServer,
) -> None:
    assert server._proposal_lock_for("a") is server._proposal_lock_for("a")
    assert server._proposal_lock_for("a") is not server._proposal_lock_for("b")


@pytest.mark.asyncio
async def test_proposal_lock_table_is_bounded(server: PerunChatMCPServer) -> None:
    for index in range(server._MAX_PROPOSAL_LOCKS + 44):
        server._proposal_lock_for(f"prop_{index}")
    assert len(server._proposal_locks) <= server._MAX_PROPOSAL_LOCKS

    held = server._proposal_lock_for("held")
    await held.acquire()
    try:
        for index in range(server._MAX_PROPOSAL_LOCKS + 44):
            server._proposal_lock_for(f"other_{index}")
        assert server._proposal_lock_for("held") is held
    finally:
        held.release()


def test_control_frame_trim_warns_once_per_episode(
    server: PerunChatMCPServer, caplog
) -> None:
    total = server._MAX_CONTROL_FRAMES + 5
    with caplog.at_level("WARNING", logger="perunchat.mcp_bridge"):
        for index in range(total):
            _push_control(
                server,
                "application/perun-execution-result",
                {"op": "execution_status", "ok": True, "proposal_id": f"p{index}"},
            )
        assert len(server._control_frames) == server._MAX_CONTROL_FRAMES
        messages = [r.getMessage() for r in caplog.records]
        assert sum("dropping" in message for message in messages) == 1

        # Drain and saturate again: a new episode warns once more.
        server._control_frames.clear()
        for index in range(total):
            _push_control(
                server,
                "application/perun-execution-result",
                {"op": "execution_status", "ok": True, "proposal_id": f"q{index}"},
            )
    messages = [r.getMessage() for r in caplog.records]
    assert sum("dropping" in message for message in messages) == 2


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
async def test_write_target_file_rejects_non_string_content(
    server: PerunChatMCPServer,
) -> None:
    server._upload_archive = AsyncMock()
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_write_target_file",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "path": "README.md",
            "content": {"a": 1},
        },
    )
    assert result["isError"] is True
    assert "content must be a string" in result["content"][0]["text"]
    server._upload_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_write_target_file_rejects_lone_surrogate(
    server: PerunChatMCPServer,
) -> None:
    server._upload_archive = AsyncMock()
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_write_target_file",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "path": "README.md",
            "content": "\ud800",
        },
    )
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "content is not valid UTF-8" in text
    assert "UnicodeEncodeError" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["UTF-8", " BASE64 "])
async def test_write_target_file_normalizes_encoding(
    server: PerunChatMCPServer, encoding: str
) -> None:
    captured: dict = {}

    async def fake_upload(client, room_id, room_type, proposal_id, archive, chunk_bytes):
        captured["archive"] = archive
        return {"ok": True, "op": "package_accepted", "proposal_id": proposal_id}

    server._upload_archive = AsyncMock(side_effect=fake_upload)
    _install_client(server, AsyncMock(return_value={}))
    body = base64.b64encode(b"hello").decode("ascii") if "BASE64" in encoding else "hello"
    result = await server.call_tool(
        "perunchat_write_target_file",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "path": "README.md",
            "content": body,
            "encoding": encoding,
        },
    )
    assert result["isError"] is False
    with zipfile.ZipFile(io.BytesIO(captured["archive"])) as archive:
        assert archive.read("README.md") == b"hello"


def test_chunk_bytes_rejects_non_integers(server: PerunChatMCPServer) -> None:
    assert server._chunk_bytes_arg(None) == server._DEFAULT_CHUNK_BYTES
    assert server._chunk_bytes_arg(-1) == -1  # _upload_archive clamps the range
    for bad in ("abc", True, 1.5, [1]):
        with pytest.raises(ValueError, match="chunk_bytes must be an integer"):
            server._chunk_bytes_arg(bad)


@pytest.mark.asyncio
async def test_chunk_bytes_error_is_clean(server: PerunChatMCPServer) -> None:
    server._upload_archive = AsyncMock()
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_write_target_file",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "path": "README.md",
            "content": "hello",
            "chunk_bytes": "abc",
        },
    )
    assert result["isError"] is True
    assert "chunk_bytes must be an integer" in result["content"][0]["text"]
    server._upload_archive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("room_type", ["GRUOP", "__members__", 1, ["GROUP"]])
async def test_unknown_room_type_is_rejected(
    server: PerunChatMCPServer, room_type
) -> None:
    client = _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_join_room",
        {"room_id": "grp:perun:demo:chat", "room_type": room_type},
    )
    assert result["isError"] is True
    assert "room_type must be one of" in result["content"][0]["text"]
    client.join_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_omitted_room_type_defaults_to_group(server: PerunChatMCPServer) -> None:
    client = _install_client(server, AsyncMock(return_value={}))
    client.leave_room = AsyncMock()
    result = await server.call_tool(
        "perunchat_leave_room", {"room_id": "grp:perun:demo:chat"}
    )
    assert result["isError"] is False
    client.leave_room.assert_awaited_once_with(
        "grp:perun:demo:chat", server._RoomType.GROUP
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("risk_tier", ["TIER_9", "tier_3_high_impact", 3])
async def test_unknown_risk_tier_is_rejected(
    server: PerunChatMCPServer, risk_tier
) -> None:
    client = _install_client(server, AsyncMock(return_value={}))
    client.submit_proposal = AsyncMock(
        return_value={"payload": {"proposal_id": "prop_test"}}
    )
    result = await server.call_tool(
        "perunchat_propose_action",
        {
            "room_id": "grp:perun:demo:chat",
            "risk_tier": risk_tier,
            "action_domain": "FILESYSTEM",
            "description": "edit README",
            "justification": "test",
        },
    )
    assert result["isError"] is True
    assert "risk_tier must be one of" in result["content"][0]["text"]
    client.submit_proposal.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("room_id", [None, "", "   ", 5])
async def test_room_id_is_required(server: PerunChatMCPServer, room_id) -> None:
    _install_client(server, AsyncMock(return_value={}))
    result = await server.call_tool(
        "perunchat_join_room", {"room_id": room_id}
    )
    assert result["isError"] is True
    assert "room_id is required" in result["content"][0]["text"]


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


def _push_package_ready(server, transfer_id: str, total_chunks: int = 3) -> None:
    _push_control(
        server,
        "application/perun-execution",
        {
            "op": "package_ready",
            "proposal_id": "prop_test",
            "transfer_id": transfer_id,
            "total_chunks": total_chunks,
        },
    )


def _push_download_chunk(
    server,
    transfer_id: str,
    seq: int,
    data: bytes = b"x",
    sender: str = "peruntheoverseer",
) -> None:
    server._on_frame(
        {
            "content_type": "application/perun-package",
            "room_id": "grp:perun:demo:chat",
            "sender": {"agent_id": sender, "runtime": "perun"},
            "payload": {
                "transfer_id": transfer_id,
                "seq": seq,
                "data": base64.b64encode(data).decode("ascii"),
            },
        }
    )


def test_download_chunk_out_of_range_is_rejected(server: PerunChatMCPServer) -> None:
    _push_package_ready(server, "trf_range", total_chunks=3)
    for seq in (3, -1):
        _push_download_chunk(server, "trf_range", seq)
    assert "trf_range" not in server._package_chunks
    assert "outside 0..2" in server._package_errors["trf_range"]


def test_download_duplicate_chunk_with_different_data_fails(
    server: PerunChatMCPServer,
) -> None:
    _push_package_ready(server, "trf_dup", total_chunks=2)
    _push_download_chunk(server, "trf_dup", 0, data=b"first")
    _push_download_chunk(server, "trf_dup", 0, data=b"second")
    assert server._package_chunks["trf_dup"][0] == b"first"
    assert "duplicate chunk 0" in server._package_errors["trf_dup"]


def test_download_chunk_from_other_sender_is_ignored(
    server: PerunChatMCPServer,
) -> None:
    _push_package_ready(server, "trf_owner", total_chunks=1)
    _push_download_chunk(server, "trf_owner", 0, sender="someone-else")
    assert "trf_owner" not in server._package_chunks


def test_drop_package_clears_byte_accounting(server: PerunChatMCPServer) -> None:
    _push_package_ready(server, "trf_bytes", total_chunks=3)
    _push_download_chunk(server, "trf_bytes", 0, data=b"aa")
    _push_download_chunk(server, "trf_bytes", 1, data=b"bbb")
    assert server._package_total_bytes == 5
    server._drop_package("trf_bytes")
    assert server._package_total_bytes == 0
    assert "trf_bytes" not in server._package_bytes
    assert "trf_bytes" not in server._expected_chunks


def test_download_slot_eviction_records_error(server: PerunChatMCPServer) -> None:
    for index in range(server._MAX_PACKAGE_TRANSFERS):
        _push_package_ready(server, f"trf_{index}", total_chunks=1)
    first = "trf_0"
    _push_package_ready(server, "trf_overflow", total_chunks=1)
    assert first not in server._expected_downloads
    assert "evicted" in server._package_errors[first]


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
async def test_download_sha_mismatch_removes_partial(
    server: PerunChatMCPServer, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PERUNCHAT_HANDOFF_DIR", str(tmp_path))
    archive = _zip_bytes("README.md", b"hello\n")
    transfer_id = "trf_badsha"
    server._package_chunks[transfer_id] = {0: archive}
    server._package_bytes[transfer_id] = len(archive)

    async def send(room_id, room_type, content_type, payload, **kwargs):
        _push_control(
            server,
            "application/perun-execution",
            {
                "op": "package_ready",
                "proposal_id": "prop_test",
                "transfer_id": transfer_id,
                "sha256": "0" * 64,
                "total_size": len(archive),
                "total_chunks": 1,
            },
        )
        return {}

    _install_client(server, send)
    destination = tmp_path / "bad.zip"
    result = await server.call_tool(
        "perunchat_request_download_package",
        {
            "room_id": "grp:perun:demo:chat",
            "proposal_id": "prop_test",
            "destination": str(destination),
        },
    )
    assert result["isError"] is True
    assert "sha256 mismatch" in result["content"][0]["text"]
    assert not destination.exists()
    assert transfer_id not in server._package_chunks


@pytest.mark.asyncio
async def test_gateway_error_is_surfaced(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload, **kwargs):
        server._on_frame(
            {"type": "ERROR", "message": "non_monotonic_sequence", "room_id": room_id}
        )
        return {}

    _install_client(server, send)
    result = await server.call_tool(
        "perunchat_claim_execution",
        {"room_id": "grp:perun:demo:chat", "proposal_id": "prop_test"},
    )
    assert result["isError"] is True
    assert "non_monotonic_sequence" in result["content"][0]["text"]


def test_serve_mcp_rejects_unsupported_server_api(monkeypatch) -> None:
    import perunchat.mcp_bridge as bridge

    class V2Server:
        pass

    monkeypatch.setattr(bridge, "Server", V2Server)
    monkeypatch.setattr(bridge, "_HAS_MCP", True)
    with pytest.raises(RuntimeError, match="mcp 1.x"):
        bridge.serve_mcp("ws://localhost/ws", "key", "agent-1", "stdio")


@pytest.mark.asyncio
async def test_owner_is_tracked_per_room(server: PerunChatMCPServer) -> None:
    _install_client(server, AsyncMock(return_value={}))
    for room_id, owner in (("grp:a", "owner-a"), ("grp:b", "owner-b")):
        server._on_frame(
            {
                "room_id": room_id,
                "content_type": "application/perun-execution-result",
                "sender": {"agent_id": owner, "runtime": "perun"},
                "payload": {"op": "execution_status", "ok": True},
            }
        )
    assert server._perun_agent_ids["grp:a"] == "owner-a"
    assert server._perun_agent_ids["grp:b"] == "owner-b"

    client = server._client
    await server._send_execution(
        client, "grp:a", server._RoomType.GROUP, {"op": "execution_status"}
    )
    await server._send_execution(
        client, "grp:b", server._RoomType.GROUP, {"op": "execution_status"}
    )
    recipients = [
        call.kwargs.get("recipient_id") for call in client.send_frame.await_args_list
    ]
    assert recipients == ["owner-a", "owner-b"]


@pytest.mark.asyncio
async def test_transfer_status_returns_payload(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload, **kwargs):
        _push_control(
            server,
            "application/perun-execution-result",
            {
                "op": "transfer_status",
                "transfer_id": payload["transfer_id"],
                "ok": True,
                "received_chunks": [0],
                "missing": [1],
            },
        )
        return {}

    _install_client(server, send)
    result = await server.call_tool(
        "perunchat_transfer_status",
        {"room_id": "grp:perun:demo:chat", "transfer_id": "trf_test"},
    )
    assert result["isError"] is False
    assert '"missing": [' in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_revoke_claim_returns_payload(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload, **kwargs):
        _push_control(
            server,
            "application/perun-execution-result",
            {
                "op": "revoke_execution_claim",
                "proposal_id": payload["proposal_id"],
                "ok": True,
                "proposal": {"id": payload["proposal_id"], "status": "approved"},
            },
        )
        return {}

    _install_client(server, send)
    result = await server.call_tool(
        "perunchat_revoke_execution_claim",
        {"room_id": "grp:perun:demo:chat", "proposal_id": "prop_test"},
    )
    assert result["isError"] is False
    assert '"status": "approved"' in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_execution_status_returns_payload(server: PerunChatMCPServer) -> None:
    async def send(room_id, room_type, content_type, payload, **kwargs):
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
