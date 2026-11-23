"""Spawn calls retain their identity across the MCP bridge's reconnect attempts."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import mcp_server
from looper.agents.http_server import AGENT_API, AgentHTTPServer
from looper.agents.mcp_server import AgentClient
from looper.agents.ops import RetrySafety
from looper.agents.ops.room import GrantState


@contextmanager
def _answer():
    yield SimpleNamespace(status=200, read=lambda: b'{"status":"ok"}')


@pytest.mark.parametrize("meta", [None, {"claudecode/toolUseId": "toolu-spawn"}, {"toolCallId": "call-spawn"}])
def test_spawn_bridge_retains_call_id_after_a_possibly_executed_request(monkeypatch, meta):
    client = AgentClient("http://127.0.0.1:1", "run.test", reconnect_seconds=2)
    client._retry_safety = {"looper_agent_spawn": RetrySafety.IDEMPOTENT}
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)
    calls = []

    def send(request, timeout):
        calls.append((request.get_header("X-looper-call"), json.loads(request.data)))
        if len(calls) == 1:
            raise ConnectionResetError("response lost after spawn")
        return _answer()

    client._opener.open = Mock(side_effect=send)
    arguments = {"kind": "explorer", "brief": {"goal": "read"}}
    assert client.call_tool("looper_agent_spawn", arguments, meta)["status"] == "ok"
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert calls[0][0]
    if meta:
        assert calls[0][0] == next(iter(meta.values()))
    client.call_tool("looper_agent_spawn", arguments, meta)
    if meta:
        assert calls[2][0] == calls[0][0]
    else:
        assert calls[2][0] != calls[0][0]


def test_idempotent_retry_requires_a_call_id(monkeypatch):
    client = AgentClient("http://127.0.0.1:1", "run.test", reconnect_seconds=2)
    client._retry_safety = {"write": RetrySafety.IDEMPOTENT}
    client._opener.open = Mock(side_effect=ConnectionResetError("response lost"))
    monkeypatch.setattr(mcp_server.time, "sleep", lambda _: None)
    with pytest.raises(mcp_server.LooperUnavailable):
        client.call_tool("write", {})
    assert client._opener.open.call_count == 1


@pytest.mark.parametrize("name", ["looper_agent_spawn", "looper_agent_wait"])
@pytest.mark.parametrize("cap", [None, 50])
def test_subagent_waits_receive_transport_time_and_runtime_caps(monkeypatch, name, cap):
    client = AgentClient("http://127.0.0.1:1", "run.test", timeout=5, max_wait_seconds=cap)
    tools = [{"name": name, "description": "Wait for a child.", "inputSchema": {
        "type": "object", "properties": {"wait_seconds": {"type": "integer", "maximum": 120}}}}]
    seen = []

    def request(path, body=None, **kwargs):
        seen.append((body, kwargs))
        return {"tools": tools} if body is None else {"status": "ok"}

    monkeypatch.setattr(client, "_request", request)
    listed = client.list_tools()
    assert listed[0]["inputSchema"]["properties"]["wait_seconds"]["maximum"] == (cap or 120)
    arguments = {"wait_seconds": 120}
    assert client.call_tool(name, arguments)["status"] == "ok"
    assert seen[-1][0]["wait_seconds"] == (cap or 120)
    assert seen[-1][1]["timeout"] == (cap or 120) + mcp_server.WAIT_MARGIN_SECONDS
    assert arguments == {"wait_seconds": 120}


def test_run_token_http_route_passes_the_bridged_call_id():
    ops = SimpleNamespace(grant_state=lambda _: GrantState.ACTIVE,
                          call=AsyncMock(return_value={"status": "ok"}))
    server = SimpleNamespace(_room_ops=ops)
    writer = SimpleNamespace(write=Mock(), drain=AsyncMock())
    asyncio.run(AgentHTTPServer._handle_room_api(server, "POST", f"{AGENT_API}ops/looper_agent_spawn",
        {"authorization": "Bearer run.test", "x-looper-call": "spawn-call"},
        b'{"kind":"explorer"}', writer))
    ops.call.assert_awaited_once_with("Bearer run.test", "looper_agent_spawn", {"kind": "explorer"},
                                     call_id="spawn-call")
    assert writer.write.call_args.args[0].startswith(b"HTTP/1.1 200 OK\r\n")


def test_in_process_worker_bridge_preserves_runtime_call_id():
    from looper.agents.runtimes.api.tools import RoomTurnTools
    from looper.agents.runtimes.api.loop import ToolCall
    ops = SimpleNamespace(call=AsyncMock(return_value={"status": "ok"}))
    tools = RoomTurnTools(ops, "run.synthetic")
    asyncio.run(tools.call(ToolCall("spawn-call", "looper_agent_spawn", '{"kind":"explorer"}')))
    ops.call.assert_awaited_once_with("Bearer run.synthetic", "looper_agent_spawn", {"kind": "explorer"},
                                     call_id="spawn-call")
