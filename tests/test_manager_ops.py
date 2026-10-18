"""The Manager's tool calls: argument checks, risk tiers by turn, confirmations and the audit trail (``agents.ops``)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.ops import Call, ErrorCode, OpError, Risk, Tool, Turn, check_arguments
from looper.agents.ops.service import CONFIRM, CONFIRMATION_SECONDS, ManagerOps, Platform
from looper.db import manager_actions

_SCHEMA = {"type": "object", "additionalProperties": False,
           "properties": {"n": {"type": "integer"}, "big": {"type": "boolean"}, "api_key": {"type": "string"}}}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def record(**row: Any) -> int:
        rows.append(row)
        return len(rows)

    monkeypatch.setattr(manager_actions, "record", AsyncMock(side_effect=record))
    return rows


class _Ops:
    """A ``ManagerOps`` over three stand-in tools; ``ran`` lists each run's arguments and the message of its turn."""

    def __init__(self) -> None:
        self.ran: list[tuple[dict[str, Any], str | None]] = []
        self.clock = _Clock()

        async def run(call: Call) -> dict[str, Any]:
            if call.arguments.get("n") == 13:
                raise OpError(ErrorCode.CONFLICT, "unlucky")
            if call.arguments.get("n") == 99:
                raise ConnectionError("database is down")
            self.ran.append((dict(call.arguments), call.turn.message_id))
            return {"task_id": "task-000000000001", "n": call.arguments.get("n")}

        async def escalate(call: Call) -> str | None:
            return f"删除 {call.arguments.get('n')}？" if call.arguments.get("big") else None

        self.ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok",
                              tools=(Tool("t_read", "reads", _SCHEMA, Risk.READ, run),
                                     Tool("t_write", "writes", _SCHEMA, Risk.WRITE, run, escalate=escalate,
                                          task_id=lambda arguments: "task-000000000009"),
                                     CONFIRM),
                              clock=self.clock)

    def call(self, name: str, arguments: Any) -> dict[str, Any]:
        return asyncio.run(self.ops.call(name, arguments))


def _turn(message: str | None, chat: str = "A") -> Turn:
    return Turn(channel="web", chat=chat, message_id=message)


@pytest.mark.parametrize(("arguments", "problem"), [
    ({"n": 1, "s": "x", "e": "a", "l": ["x"], "o": {"k": "v", "j": None}}, None),
    ([1], "the arguments must be a JSON object"),
    ({"n": 1, "zzz": 2}, "unknown argument 'zzz'"),
    ({}, "n is required"),
    ({"n": True}, "n must be an integer"),
    ({"n": 0}, "n must be from 1 to 5"),
    ({"n": 1, "s": "  "}, "s must not be empty"),
    ({"n": 1, "s": "abcdef"}, "s has 6 characters; at most 5 are allowed"),
    ({"n": 1, "s": 3}, "s must be a string"),
    ({"n": 1, "e": "c"}, "e must be one of: a, b"),
    ({"n": 1, "l": ["x", 2]}, "l must be a list of strings"),
    ({"n": 1, "o": {"x": "v"}}, "o has an unknown key 'x'"),
    ({"n": 1, "o": {"k": 1}}, "o.k must be a string"),
])
def test_arguments_are_checked_against_the_schema(arguments: Any, problem: str | None) -> None:
    schema = {"type": "object", "required": ["n"], "properties": {
        "n": {"type": "integer", "minimum": 1, "maximum": 5}, "s": {"type": "string", "minLength": 1, "maxLength": 5},
        "e": {"type": "string", "enum": ["a", "b"]}, "l": {"type": "array"},
        "o": {"type": "object", "properties": {"k": {"type": "string"}, "j": {"type": "string"}}}}}
    if problem is None:
        assert check_arguments(schema, arguments) == arguments
        return
    with pytest.raises(OpError) as raised:
        check_arguments(schema, arguments)
    assert raised.value.code is ErrorCode.INVALID and str(raised.value).startswith(problem)


@pytest.mark.parametrize("name", ["api_key", "OPENAI_KEY", "token", "password", "client_secret", "credential"])
def test_an_argument_named_like_a_secret_is_refused_and_its_value_never_named(name: str) -> None:
    with pytest.raises(OpError) as raised:
        check_arguments({"type": "object", "properties": {"n": {"type": "integer"}}}, {"n": 1, name: "sk-x"})
    assert raised.value.code is ErrorCode.FORBIDDEN
    assert str(raised.value).startswith(f"{name}: Looper's tools never take a key") and "sk-x" not in str(raised.value)
    assert "Settings → LLM Providers" in str(raised.value)


@pytest.mark.parametrize(("header", "allowed"), [
    ("Bearer tok", True), ("bearer tok", True), ("Bearer  tok ", True),
    ("Bearer other", False), ("tok", False), ("Basic tok", False), ("", False), (None, False),
])
def test_only_the_servers_token_gets_in(header: str | None, allowed: bool) -> None:
    assert _Ops().ops.authorized(header) is allowed


def test_the_listing_is_the_catalog() -> None:
    names = [tool["name"] for tool in _Ops().ops.listing()]
    assert names == ["t_read", "t_write", "looper_confirm"]
    assert _Ops().ops.listing()[0]["inputSchema"] == _SCHEMA


@pytest.mark.parametrize("turn", [None, _turn(None)], ids=["no-turn", "own-turn"])
def test_outside_a_turn_answering_the_user_only_reads(recorded: list[dict[str, Any]], turn: Turn | None) -> None:
    ops = _Ops()
    if turn is not None:
        ops.ops.begin_turn(turn)
    assert ops.call("t_read", {"n": 1})["status"] == "ok"
    answer = ops.call("t_write", {"n": 2})
    assert answer["error"]["code"] == "needs_user_turn"
    assert ops.ran == [({"n": 1}, None)]
    # A read's result is not kept; every call is in the trail.
    assert [(r["tool"], r["risk"], r["status"], r["result"]) for r in recorded] == [
        ("t_read", "read", "ok", None),
        ("t_write", "write", "error", {"message": answer["error"]["message"]})]


def test_a_tools_read_actions_run_in_any_turn_and_its_others_only_answering_the_user(
    recorded: list[dict[str, Any]],
) -> None:
    async def run(call: Call) -> dict[str, Any]:
        return {"rows": [1, 2]}

    schema = {"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "create"]}}}
    tool = Tool("t_mixed", "lists and creates", schema, Risk.WRITE, run, read_actions=frozenset({"list"}))
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", token="tok", tools=(tool,))
    ops.begin_turn(_turn(None))
    assert asyncio.run(ops.call("t_mixed", {"action": "list"}))["status"] == "ok"
    assert asyncio.run(ops.call("t_mixed", {"action": "create"}))["error"]["code"] == "needs_user_turn"
    ops.begin_turn(_turn("m1"))
    assert asyncio.run(ops.call("t_mixed", {"action": "create"}))["status"] == "ok"
    # A read's result is not kept, a change's is.
    assert [(r["risk"], r["status"], r["result"]) for r in recorded] == [
        ("read", "ok", None), ("write", "error", {"message": recorded[1]["result"]["message"]}),
        ("write", "ok", {"rows": [1, 2]})]


def test_a_write_runs_in_the_turn_answering_the_user(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    turn = _turn("m1")
    assert ops.ops.begin_turn(turn) == ""
    assert ops.call("t_write", {"n": 2, "api_key": "sk-secret"}) == {
        "status": "ok", "result": {"task_id": "task-000000000001", "n": 2}}
    (row,) = recorded
    assert (row["channel"], row["chat"], row["message_id"], row["risk"], row["task_id"]) == (
        "web", "A", "m1", "write", "task-000000000009")
    assert manager_actions.masked(row["arguments"]) == {"n": 2, "api_key": "***"}


def test_a_turn_collects_every_calls_task_target(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops().ops
    turn = _turn("m1")
    tool = Tool("t_read", "reads", {}, Risk.READ, AsyncMock(), task_id=lambda arguments: arguments.get("task_id"))

    async def test() -> None:
        await ops._record(turn, tool, {"task_id": "task-a"}, Risk.READ, "ok")
        await ops._record(turn, tool, {"task_id": "task-a"}, Risk.READ, "ok")
        assert turn.task_ids == {"task-a"}
        await ops._record(turn, tool, {}, Risk.WRITE, "ok", result={"task_id": "task-b"})
        await ops._record(turn, tool, {}, Risk.READ, "ok")
        assert turn.task_ids == {"task-a", None}
        assert _turn("m2").task_ids == set()

    asyncio.run(test())
    assert [row["task_id"] for row in recorded] == ["task-a", "task-a", "task-b", None]


@pytest.mark.parametrize(("task_id", "record_id", "task_ids", "linked"), [
    (None, 1, {"task-a"}, True),
    (None, 1, {"task-a", "task-b"}, False),
    (None, 1, {"task-a", None}, False),
    (None, 1, {None}, False),
    (None, 1, set(), False),
    ("task-b", 1, {"task-a"}, False),
    (None, None, {"task-a"}, False),
])
def test_a_manager_turn_links_only_one_named_task_when_its_record_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, task_id: str | None, record_id: int | None,
    task_ids: set[str | None], linked: bool,
) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import llm_records

    link = AsyncMock()
    monkeypatch.setattr(llm_records, "link_task", link)
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    recorder = SimpleNamespace(task_id=task_id, record_id=record_id)
    asyncio.run(manager._link_turn_task(recorder, Turn(channel="web", chat="A", task_ids=task_ids)))
    if linked:
        link.assert_awaited_once_with(record_id, "task-a")
    else:
        link.assert_not_awaited()


def test_a_failure_to_link_a_manager_turn_logs_and_keeps_the_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import llm_records

    monkeypatch.setattr(llm_records, "link_task", AsyncMock(side_effect=ConnectionError("database is down")))
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    asyncio.run(manager._link_turn_task(SimpleNamespace(task_id=None, record_id=1),
                                       Turn(channel="web", chat="A", task_ids={"task-a"})))
    assert "could not link the Manager's turn to its task" in caplog.text


@pytest.mark.parametrize(("create", "global_call", "linked"), [
    (False, False, True), (False, True, False), (True, False, False), (True, True, False),
])
def test_a_manager_turn_links_only_when_every_call_names_an_existing_task(
    recorded: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    create: bool, global_call: bool, linked: bool,
) -> None:
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.db import llm_records

    link = AsyncMock()
    monkeypatch.setattr(llm_records, "link_task", link)
    tool = Tool("t_task", "reads or creates", {"type": "object", "properties": {"task_id": {"type": "string"}}},
                Risk.WRITE, AsyncMock(return_value={"task_id": "task-a"}),
                task_id=lambda arguments: arguments.get("task_id"))
    global_tool = Tool("t_global", "reads", {"type": "object"}, Risk.READ, AsyncMock(return_value={}))
    ops = ManagerOps(Platform(bus=None, task_router=None), url="http://127.0.0.1:9/", tools=(tool, global_tool))
    manager = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    turn = _turn("m1")

    async def test() -> None:
        ops.begin_turn(turn)
        assert (await ops.call("t_task", {} if create else {"task_id": "task-a"}))["status"] == "ok"
        if global_call:
            assert (await ops.call("t_global", {}))["status"] == "ok"
        ops.end_turn(turn)
        await manager._link_turn_task(SimpleNamespace(task_id=None, record_id=1), turn)

    asyncio.run(test())
    assert recorded[0]["task_id"] == "task-a"  # A create still names the new task in the audit trail.
    if linked:
        link.assert_awaited_once_with(1, "task-a")
    else:
        link.assert_not_awaited()


def test_a_high_call_runs_only_when_the_next_message_in_its_chat_confirms_it(
    recorded: list[dict[str, Any]],
) -> None:
    ops = _Ops()
    first = _turn("m1")
    ops.ops.begin_turn(first)
    asked = ops.call("t_write", {"n": 3, "big": True})
    ops.ops.end_turn(first)
    assert asked["status"] == "needs_confirmation" and asked["expires_in_seconds"] == CONFIRMATION_SECONDS
    assert ops.ran == []
    # Looper shows the user what it does, in its own words, after Claude's reply.
    assert first.blocks == ["需要你确认：\n删除 3？\n回复「确认」就执行；回复别的就不执行。"]

    other = _turn("b1", chat="B")  # another chat speaking neither drops it nor can confirm it
    assert ops.ops.begin_turn(other) == ""
    assert ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})["error"]["code"] == "not_found"
    ops.ops.end_turn(other)

    second = _turn("m2")
    note = ops.ops.begin_turn(second)
    assert asked["confirmation_id"] in note and "删除 3？" in note
    done = ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})
    ops.ops.end_turn(second)
    assert done == {"status": "ok", "result": {"task_id": "task-000000000001", "n": 3}}
    # It runs once, as it was asked, in the confirming turn.
    assert ops.ran == [({"n": 3, "big": True}, "m2")]
    statuses = [(r["tool"], r["risk"], r["status"], r["confirmation_id"]) for r in recorded]
    assert statuses[0] == ("t_write", "high", "needs_confirmation", asked["confirmation_id"])
    assert statuses[-1] == ("t_write", "high", "ok", asked["confirmation_id"])
    assert ops.call("looper_confirm", {"confirmation_id": asked["confirmation_id"]})["error"]["code"] in (
        "needs_user_turn", "not_found")


@pytest.mark.parametrize("how", ["next-message-did-not-confirm", "too-late", "own-turn"])
def test_a_confirmation_lapses(recorded: list[dict[str, Any]], how: str) -> None:
    ops = _Ops()
    first = _turn("m1")
    ops.ops.begin_turn(first)
    confirmation = ops.call("t_write", {"n": 3, "big": True})["confirmation_id"]
    ops.ops.end_turn(first)
    if how == "next-message-did-not-confirm":
        ops.ops.begin_turn(second := _turn("m2"))
        ops.ops.end_turn(second)
        ops.ops.begin_turn(_turn("m3"))
    elif how == "too-late":
        ops.clock.now += CONFIRMATION_SECONDS + 1
        assert ops.ops.begin_turn(_turn("m2")) == ""
    else:
        ops.ops.begin_turn(_turn(None))
    answer = ops.call("looper_confirm", {"confirmation_id": confirmation})
    assert answer["status"] == "error" and ops.ran == []


def test_a_new_high_call_replaces_the_one_waiting(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    turn = _turn("m1")
    ops.ops.begin_turn(turn)
    first = ops.call("t_write", {"n": 1, "big": True})["confirmation_id"]
    second = ops.call("t_write", {"n": 2, "big": True})["confirmation_id"]
    ops.ops.end_turn(turn)
    assert turn.blocks == ["需要你确认：\n删除 2？\n回复「确认」就执行；回复别的就不执行。"]
    ops.ops.begin_turn(_turn("m2"))
    assert ops.call("looper_confirm", {"confirmation_id": first})["error"]["code"] == "not_found"
    assert ops.call("looper_confirm", {"confirmation_id": second})["status"] == "ok"
    assert ops.ran == [({"n": 2, "big": True}, "m2")]


def test_a_reset_drops_every_waiting_confirmation(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    ops.ops.begin_turn(turn := _turn("m1"))
    confirmation = ops.call("t_write", {"n": 1, "big": True})["confirmation_id"]
    ops.ops.end_turn(turn)
    ops.ops.forget_confirmations()
    assert ops.ops.begin_turn(_turn("m2")) == ""
    assert ops.call("looper_confirm", {"confirmation_id": confirmation})["status"] == "error"


@pytest.mark.parametrize(("name", "arguments", "code"), [
    ("nope", {}, "invalid"),
    ("t_write", {"n": "x"}, "invalid"),
    ("t_write", {"n": 13}, "conflict"),
    ("t_write", {"n": 99}, "unavailable"),
    ("t_write", None, "invalid"),
])
def test_a_refused_or_failed_call_is_an_error_answer(
    recorded: list[dict[str, Any]], name: str, arguments: Any, code: str,
) -> None:
    ops = _Ops()
    ops.ops.begin_turn(_turn("m1"))
    answer = ops.call(name, arguments)
    assert answer["status"] == "error" and answer["error"]["code"] == code and answer["error"]["message"]
    # An unknown tool is no call of the catalog; every other is in the trail with its code.
    assert [r["error_code"] for r in recorded] == ([] if name == "nope" else [code])


def test_a_confirmed_call_that_fails_is_recorded_once(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    ops.ops.begin_turn(turn := _turn("m1"))
    confirmation = ops.call("t_write", {"n": 13, "big": True})["confirmation_id"]
    ops.ops.end_turn(turn)
    ops.ops.begin_turn(_turn("m2"))
    answer = ops.call("looper_confirm", {"confirmation_id": confirmation})
    assert answer["error"] == {"code": "conflict", "message": "unlucky"}
    assert [(r["status"], r["confirmation_id"]) for r in recorded] == [
        ("needs_confirmation", confirmation), ("error", confirmation)]


def test_a_trail_that_cannot_be_written_does_not_fail_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_actions, "record", AsyncMock(side_effect=ConnectionError("down")))
    ops = _Ops()
    ops.ops.begin_turn(_turn("m1"))
    assert ops.call("t_write", {"n": 1})["status"] == "ok"


@pytest.mark.parametrize(("value", "expected"), [
    ({"api_key": "x", "nested": {"token": "y", "name": "n"}, "list": [{"password": "z"}]},
     {"api_key": "***", "nested": {"token": "***", "name": "n"}, "list": [{"password": "***"}]}),
    ({"secret": None}, {"secret": None}),
    ({"credential": "c", "env": {"MY_CREDENTIALS": "d"}}, {"credential": "***", "env": {"MY_CREDENTIALS": "***"}}),
])
def test_secret_values_are_masked(value: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    assert manager_actions.masked(value) == expected


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass


@pytest.mark.parametrize(("method", "path", "token", "body", "status", "answer"), [
    ("GET", "/api/agent/v1/tools", None, b"", 401, None),
    ("GET", "/api/agent/v1/tools", "Bearer other", b"", 401, None),
    ("POST", "/api/agent/v1/ops/t_read", None, b"{}", 401, None),
    ("GET", "/api/agent/v1/tools", "Bearer tok", b"", 200, "tools"),
    ("POST", "/api/agent/v1/ops/t_read", "Bearer tok", b'{"n": 1}', 200, "ok"),
    ("POST", "/api/agent/v1/ops/t_read", "Bearer tok", b"", 200, "ok"),  # no body: no arguments
    ("POST", "/api/agent/v1/ops/t_read", "Bearer tok", b"{not json", 200, "invalid"),
    ("POST", "/api/agent/v1/ops/nope", "Bearer tok", b"{}", 200, "invalid"),
    ("GET", "/api/agent/v1/ops/t_read", "Bearer tok", b"", 404, None),
])
def test_the_endpoint_takes_only_the_managers_token(
    recorded: list[dict[str, Any]], method: str, path: str, token: str | None, body: bytes, status: int,
    answer: str | None,
) -> None:
    from types import SimpleNamespace

    from looper.agents.http_server import AgentHTTPServer

    ops = _Ops()
    server = AgentHTTPServer(web_adapter=SimpleNamespace(), supervisor=SimpleNamespace(),
                             task_router=SimpleNamespace(), manager_ops=ops.ops)
    writer = _Writer()
    headers = {"authorization": token} if token else {}
    asyncio.run(server._handle_agent_api(method, path, headers, body, writer))  # type: ignore[arg-type]
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    assert int(head.split()[1]) == status
    data = json.loads(payload)
    if answer == "tools":
        assert [tool["name"] for tool in data["tools"]] == ["t_read", "t_write", "looper_confirm"]
    elif answer == "ok":
        assert data["status"] == "ok"
    elif answer is not None:
        assert data["error"]["code"] == answer
    if status != 200:
        assert ops.ran == [] and "error" in data


def test_the_managers_launch_gets_loopers_server_over_a_catalog_one_of_that_name(tmp_path: Any) -> None:
    import sys

    from looper.agents.library import AgentLibrary, ItemKind, LoadedItem
    from looper.agents.mcp import write_config
    from looper.agents.runtimes.codex import mcp_servers as codex_servers
    from looper.agents.runtimes.omp import mcp_config as omp_config

    installed = write_config(tmp_path, [("notes", {"command": "notes-mcp"}), ("looper", {"command": "impostor"})])
    library = AgentLibrary(items=(LoadedItem(ItemKind.MCP, "notes", "a" * 64, plugin=""),
                                  LoadedItem(ItemKind.MCP, "looper", "b" * 64, plugin="")),
                           mcp_config=str(installed), role="manager")
    ops = _Ops().ops
    launched = asyncio.run(ops.library_with_server(library, tmp_path))
    servers = json.loads(open(launched.mcp_config, encoding="utf-8").read())["mcpServers"]
    assert servers["notes"] == {"command": "notes-mcp"}
    assert servers["looper"] == {"command": sys.executable, "args": ["-m", "looper.cli", "mcp-server"],
                                 "env": {"LOOPER_AGENT_URL": "http://127.0.0.1:9", "LOOPER_AGENT_TOKEN": "tok"}}
    assert [(item.name, item.scope) for item in launched.items] == [("notes", "global"), ("looper", "builtin")]
    assert launched.record()["mcp_servers"] == ["notes", "looper"]
    # Codex and OMP read the same file: a stdio server with its environment.
    assert dict(codex_servers(launched)[0])["looper"]["env"]["LOOPER_AGENT_TOKEN"] == "tok"
    assert omp_config(launched, {})["mcpServers"]["looper"]["args"] == ["-m", "looper.cli", "mcp-server"]
    # A Manager with nothing installed gets it too.
    alone = asyncio.run(ops.library_with_server(AgentLibrary(role="manager"), tmp_path))
    assert list(json.loads(open(alone.mcp_config, encoding="utf-8").read())["mcpServers"]) == ["looper"]
