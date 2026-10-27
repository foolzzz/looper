"""In-process Manager tools preserve the bridge's answers, turn checks and audit trail."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import mcp_server
from looper.agents.ops import ErrorCode, OpError, Turn
from looper.agents.ops.service import ManagerOps, Platform
from looper.agents.runtimes.api.loop import ToolCall, ToolOutcome, ToolSpec
from looper.agents.runtimes.api.tools import ManagerTurnTools, PlannerTurnTools
from looper.agents.runtimes.runner.turn import RunnerTurn
from tests.db_support import run_db


async def _limits():
    return {"run_cap": None, "task_cap": None, "task_spent": 0}


class _RunnerEntry:
    def __init__(self, ops, binding):
        self.turn = RunnerTurn(session_id="session", trace_id="trace", role="manager",
                               tools=ManagerTurnTools(ops, binding), provider_id="provider", model="model",
                               price=None, limits=_limits)

    async def call(self, call):
        try:
            arguments = json.loads(call.arguments or "{}")
        except ValueError:
            arguments = None
        result = await self.turn.call_tool({"session_id": "session", "trace_id": "trace",
                                           "call_id": call.call_id, "name": call.name, "arguments": arguments,
                                           "binding": "untrusted", "role": "planner", "token": "untrusted"})
        return ToolOutcome(result["text"], result["is_error"], result["ran"])


@pytest.fixture(params=[ManagerTurnTools, _RunnerEntry], ids=["api", "runner"])
def turn_tools(request):
    return request.param


def _ops() -> ManagerOps:
    return ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9", token="test-token")


def _call(name: str, arguments: Any, call_id: str = "call-1") -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


@pytest.mark.parametrize("answer", [
    {"status": "ok", "result": {"text": "项目"}},
    {"status": "needs_confirmation", "confirmation_id": "confirm-test"},
    {"status": "error", "error": {"code": "invalid", "message": "wrong action"}},
])
def test_answers_match_the_mcp_tool_text(answer: dict[str, Any], turn_tools: Any) -> None:
    ops = SimpleNamespace(call=AsyncMock(return_value=answer))
    outcome = asyncio.run(turn_tools(ops, None).call(_call("looper_projects", {})))
    bridge = mcp_server._call_tool("request-1", {"name": "looper_projects", "arguments": {}},
                                   SimpleNamespace(call_tool=lambda *args: answer))["result"]
    assert outcome.text == bridge["content"][0]["text"]
    assert outcome.is_error == bridge["isError"]
    assert outcome.ran


def test_specs_preserve_the_current_platform_listing() -> None:
    ops = _ops()

    async def test() -> None:
        expected = [ToolSpec(t["name"], t["description"], t["inputSchema"]) for t in await ops.current_listing()]
        assert await ManagerTurnTools(ops, None).specs() == expected

    asyncio.run(test())


def test_a_listing_failure_propagates() -> None:
    ops = SimpleNamespace(current_listing=AsyncMock(side_effect=OpError(ErrorCode.UNAVAILABLE, "offline")))
    with pytest.raises(OpError, match="offline"):
        asyncio.run(ManagerTurnTools(ops, None).specs())


@pytest.mark.parametrize("arguments", ["not JSON", "[]"])
def test_bad_arguments_do_not_call_platform_ops_or_write_an_action(
    monkeypatch: pytest.MonkeyPatch, arguments: str, turn_tools: Any,
) -> None:
    async def test(pool: Any) -> None:
        ops = _ops()
        called = AsyncMock(wraps=ops.call)
        monkeypatch.setattr(ops, "call", called)
        result = await turn_tools(ops, "binding").call(ToolCall("call-1", "looper_projects", arguments))
        assert json.loads(result.text) == {"status": "error", "error": {
            "code": "invalid", "message": "the arguments must be a JSON object"}}
        assert result.is_error and not result.ran
        called.assert_not_awaited()
        assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0

    run_db(test, monkeypatch, timeout=30)


def test_an_empty_argument_string_is_an_object() -> None:
    async def call(name: str, arguments: Any, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "result": arguments}

    outcome = asyncio.run(ManagerTurnTools(SimpleNamespace(call=call), None).call(
        ToolCall("call-id", "looper_overview", "")))
    assert json.loads(outcome.text) == {"status": "ok", "result": {}}


@pytest.mark.parametrize("text", ["中" * 100, "中" * 34_120, "中" * 40_000])
def test_tool_results_fit_the_utf8_byte_bound(text: str, turn_tools: Any) -> None:
    answer = {"status": "ok", "result": text}
    original = json.dumps(answer, ensure_ascii=False)
    size = len(original.encode("utf-8"))
    ops = SimpleNamespace(call=AsyncMock(return_value=answer))
    result = asyncio.run(turn_tools(ops, None).call(_call("looper_projects", {})))
    assert len(result.text.encode("utf-8")) <= 102_400
    if size <= 102_400:
        assert result.text == original
    else:
        note = (f"\n[Looper] This result was cut from {size} bytes to 100 KB. "
                "Ask for less (a filter, one task id).")
        assert result.text.endswith(note)
        prefix = result.text[:-len(note)]
        assert original.startswith(prefix)
        # A three-byte character cannot fit in the remaining one or two bytes.
        assert len((prefix + original[len(prefix)] + note).encode("utf-8")) > 102_400
    assert "\ufffd" not in result.text


async def _post(server: Any, name: str, arguments: Any) -> dict[str, Any]:
    body = json.dumps(arguments).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", server._port)
    try:
        writer.write((f"POST /api/agent/v1/ops/{name} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                      "Authorization: Bearer test-token\r\nX-Looper-Turn: turn-binding\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)
        head, _, payload = response.partition(b"\r\n\r\n")
        assert int(head.split()[1]) == 200
        return json.loads(payload)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("arguments", [{"action": "list"}, {"action": "get", "project": "sample"},
                                        {"action": "invalid"}])
def test_in_process_calls_match_http_answers_and_audit_rows(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], turn_tools: Any,
) -> None:
    from looper.agents.http_server import AgentHTTPServer
    from looper.db import projects

    async def test(pool: Any) -> None:
        await projects.create_project(name="sample", work_dir="/srv/sample", pool=pool)
        ops = _ops()
        ops.begin_turn(Turn(channel="web", chat="chat-1", message_id="message-1"), "turn-binding")
        tools = turn_tools(ops, "turn-binding")
        server = AgentHTTPServer(host="127.0.0.1", port=0, web_adapter=SimpleNamespace(),
                                 task_router=SimpleNamespace(), supervisor=SimpleNamespace(), manager_ops=ops)
        await server.start()
        try:
            in_process = json.loads((await tools.call(_call("looper_projects", arguments))).text)
            over_http = await _post(server, "looper_projects", arguments)
            assert in_process == over_http
            rows = [dict(row) for row in await pool.fetch("SELECT * FROM manager_actions ORDER BY id")]
            assert len(rows) == 2
            for row in rows:
                row.pop("id")
                row.pop("created_at")
            assert rows[0] == rows[1]
        finally:
            await server.stop()

    run_db(test, monkeypatch, timeout=30)


def test_unknown_tools_are_refused_by_ops_without_an_audit_row(
    monkeypatch: pytest.MonkeyPatch, turn_tools: Any,
) -> None:
    async def test(pool: Any) -> None:
        outcome = await turn_tools(_ops(), None).call(_call("looper_unknown", {}))
        assert json.loads(outcome.text)["error"]["code"] == "invalid"
        assert outcome.is_error
        assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0

    run_db(test, monkeypatch, timeout=30)


def test_a_project_is_deleted_only_when_the_next_user_turn_confirms(
    monkeypatch: pytest.MonkeyPatch, turn_tools: Any,
) -> None:
    from looper.db import projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="sample", work_dir="/srv/sample", pool=pool)
        ops = _ops()
        first = Turn(channel="web", chat="chat-1", message_id="message-1")
        ops.begin_turn(first, "binding-1")
        asked = json.loads((await turn_tools(ops, "binding-1").call(
            _call("looper_projects", {"action": "delete", "project": "sample"}))).text)
        assert asked["status"] == "needs_confirmation"
        assert len(first.blocks) == 1 and "sample" in first.blocks[0]
        assert await projects.get_project(project["id"], pool=pool) is not None
        ops.end_turn(first)
        second = Turn(channel="web", chat="chat-1", message_id="message-2")
        assert asked["confirmation_id"] in ops.begin_turn(second, "binding-2")
        confirmed = json.loads((await turn_tools(ops, "binding-2").call(
            _call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))).text)
        assert confirmed == {"status": "ok", "result": {"deleted": "sample"}}
        assert await projects.get_project(project["id"], pool=pool) is None
        rows = await pool.fetch("SELECT status, arguments FROM manager_actions ORDER BY id")
        assert [r["status"] for r in rows] == ["needs_confirmation", "ok"]
        assert json.loads(rows[1]["arguments"]) == {"action": "delete", "project": "sample"}

    run_db(test, monkeypatch, timeout=30)


def test_an_autonomous_turn_cannot_confirm_a_project_deletion(
    monkeypatch: pytest.MonkeyPatch, turn_tools: Any,
) -> None:
    from looper.db import projects

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="sample", pool=pool)
        ops = _ops()
        first = Turn(channel="web", chat="chat-1", message_id="message-1")
        ops.begin_turn(first)
        asked = json.loads((await turn_tools(ops, None).call(
            _call("looper_projects", {"action": "delete", "project": "sample"}))).text)
        ops.end_turn(first)
        ops.begin_turn(Turn(channel="web", chat="chat-1"))
        answer = await turn_tools(ops, None).call(
            _call("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
        assert json.loads(answer.text)["error"]["code"] == "needs_user_turn"
        assert await projects.get_project(project["id"], pool=pool) is not None

    run_db(test, monkeypatch, timeout=30)


def test_an_in_process_write_runs_with_stop_enabled_and_its_turn_binding(
    monkeypatch: pytest.MonkeyPatch, turn_tools: Any,
) -> None:
    from looper.agents import chat_settings
    from looper.db import projects

    async def test(pool: Any) -> None:
        monkeypatch.setattr(chat_settings, "_stop_enabled", True)
        ops = _ops()
        ops.begin_turn(Turn(channel="web", chat="chat-1", message_id="message-1", runtime="api",
                            require_match=False), "turn-binding")
        outcome = await turn_tools(ops, "turn-binding").call(
            _call("looper_projects", {"action": "create", "name": "sample"}))
        assert json.loads(outcome.text)["status"] == "ok"
        assert await projects.find_project_by_name("sample", pool=pool) is not None
        assert await pool.fetchval("SELECT status FROM manager_actions") == "ok"

    run_db(test, monkeypatch, timeout=30)


def test_runner_planner_uses_host_token_and_task_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops.planner import PlannerOps
    from looper.db import projects, tasks

    async def test(pool: Any) -> None:
        project = await projects.create_project(name="sample", work_dir="/srv/sample", pool=pool)
        await tasks.create_task(task_id="task-a", requirement="plan", project_id=project["id"],
                                work_dir="/srv/sample", status="running", pool=pool)
        ops = PlannerOps(SimpleNamespace(), url="http://127.0.0.1:9")
        token = ops.issue("task-a")
        ops.begin_turn(token)
        adapter = PlannerTurnTools(ops, token)
        assert await adapter.specs() == [ToolSpec(t["name"], t["description"], t["inputSchema"])
                                         for t in await ops.listing("Bearer " + token)]
        runner = RunnerTurn(session_id="s", trace_id="t", role="planner", tools=adapter,
                            provider_id="p", model="m", price=None, limits=_limits)

        async def call(arguments):
            return await runner.call_tool({"session_id": "s", "trace_id": "t", "call_id": "c",
                                           "name": "looper_task_profiles", "arguments": arguments,
                                           "token": "forged", "role": "manager"})

        allowed = await call({})
        assert json.loads(allowed["text"])["status"] == "ok" and allowed["ran"]
        denied = await call({"task_id": "another-task"})
        assert json.loads(denied["text"])["error"]["code"] == "forbidden"
        assert denied["ran"] and denied["is_error"]
        ops.revoke(token)
        denied = await call({})
        assert json.loads(denied["text"])["error"]["code"] == "forbidden"
        assert token not in denied["text"]
        assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 0

    run_db(test, monkeypatch, timeout=30)
