"""Run grants survive restart without serving room tools before adoption finishes."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents.http_server import AGENT_API, AgentHTTPServer
from looper.agents.ops import room as room_ops, vault as vault_ops
from looper.agents.room import MemberKind, Seat
from looper.agents.vault import VaultConfig


@pytest.mark.parametrize("kind", [MemberKind.WORK, MemberKind.REVIEW])
def test_a_grant_is_pending_until_its_run_is_adopted(tmp_path, kind):
    seat = Seat("worker@s1", "worker", kind, "step-1", "s1", "worker-1", str(tmp_path))
    original = room_ops.RoomOps(Mock(), url="http://localhost:1234")
    token = original.issue("task-1", seat, vault=VaultConfig(tmp_path, ("notes",), None))
    header = "Bearer " + token
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text(token)
    original._files[seat.run] = {str(mcp_file)}
    original._grant(header).usage.written_bytes = 42

    record = json.loads(json.dumps(original.export_run(seat.run)))
    assert token not in json.dumps(record)
    assert original.export_run("missing") is None
    assert record["format"] == 1
    assert record["vault_tools"] == [tool.name for tool in vault_ops.TOOLS]
    record["vault_tools"].append("an_unknown_old_tool")

    restored = room_ops.RoomOps(Mock(), url="http://localhost:1234")
    restored.import_pending(record)
    assert restored.grant_state(header) is room_ops.GrantState.PENDING_ADOPTION
    assert restored._grant(header).seat == seat
    assert restored._grant(header).usage.written_bytes == 0
    assert restored._grant(header).vault_tools == vault_ops.TOOLS
    assert restored._files[seat.run] == {str(mcp_file)}

    restored.activate_run(seat.run)
    assert restored.grant_state(header) is room_ops.GrantState.ACTIVE
    assert restored.listing(header) == original.listing(header)
    restored.revoke_run(seat.run)
    assert restored.grant_state(header) is None
    assert not mcp_file.exists()

    restored.import_pending(record)
    restored.revoke_run(seat.run)
    assert restored.grant_state(header) is None


@pytest.mark.parametrize(("method", "path"), [
    ("GET", f"{AGENT_API}tools"),
    ("POST", f"{AGENT_API}looper_room_post"),
])
def test_the_room_api_retries_pending_grants_and_refuses_revoked_grants(method, path):
    ops = room_ops.RoomOps(Mock(), url="http://localhost:1234")
    token = ops.issue("task-1", Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1"))
    restored = room_ops.RoomOps(Mock(), url="http://localhost:1234")
    restored.import_pending(ops.export_run("worker-1"))
    server = SimpleNamespace(_room_ops=restored)
    headers = {"authorization": "Bearer " + token}

    async def request():
        writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
        await AgentHTTPServer._handle_room_api(server, method, path, headers, b"{}", writer)
        return writer.write.call_args.args[0]

    response = asyncio.run(request())
    assert response.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
    assert b"Retry-After: 2\r\n" in response
    assert json.loads(response.split(b"\r\n\r\n", 1)[1]) == {
        "error": "this run is being adopted after a restart; retry shortly"}

    restored.activate_run("worker-1")
    if method == "GET":
        assert asyncio.run(request()).startswith(b"HTTP/1.1 200 OK\r\n")
    restored.revoke_run("worker-1")
    assert asyncio.run(request()).startswith(b"HTTP/1.1 401 Unauthorized\r\n")


@pytest.mark.parametrize(("field", "value"), [
    ("format", True), ("format", 2), ("digest", "not-a-digest"), ("digest", "g" * 64),
    ("task_id", "another-task"), ("room", 1), ("vault_tools", "looper_vault_read"),
    ("vault_tools", [1]), ("mcp_files", None), ("mcp_files", [1]),
    ("seat", None), ("seat.run", "another-run"), ("seat.key", 1),
    ("seat.kind", "unknown"), ("seat.kind", []), ("seat.step_id", 1),
    ("seat.extra", "unexpected"),
])
def test_grant_validation_rejects_malformed_or_misbound_records(field, value):
    ops = room_ops.RoomOps(Mock(), url="http://localhost:1234")
    ops.issue("task-1", Seat("worker@s1", "worker", MemberKind.WORK, run="worker-1"))
    record = ops.export_run("worker-1")
    room_ops.validate_grant(record, run_id="worker-1", task_id="task-1")
    target, key = (record["seat"], field[5:]) if field.startswith("seat.") else (record, field)
    target[key] = value
    with pytest.raises(ValueError):
        room_ops.validate_grant(record, run_id="worker-1", task_id="task-1")


@pytest.mark.parametrize("record", [None, [], {}, {"format": 1}])
def test_grant_validation_rejects_missing_records(record):
    with pytest.raises(ValueError):
        room_ops.validate_grant(record, run_id="worker-1", task_id="task-1")
