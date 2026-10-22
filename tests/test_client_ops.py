"""An API client's operations (``agents.ops.clients``): one request is one turn, writes run, ``high`` waits for
``confirm`` from the same client and session. No database: the trail is a list."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents.api_v1 import OPERATOR, Principal
from looper.agents.ops import API_CHANNEL, Call, ErrorCode, OpError, Risk, Tool, Turn
from looper.agents.ops.clients import CLIENT_TOOLS, ClientOps
from looper.agents.ops.service import CONFIRMATION_CHATS, CONFIRMATION_SECONDS, MANAGER_TOOLS, TASKS_SCOPE_TOOLS, Platform
from looper.agents.ops.tasks import _via
from looper.db import manager_actions

ALICE = Principal("client", "client-aaa", "alice")
BOB = Principal("client", "client-bbb", "bob")
_SCHEMA = {"type": "object", "additionalProperties": False,
           "properties": {"n": {"type": "integer"}, "big": {"type": "boolean"}, "note": {"type": "boolean"},
                          "secret_field": {"type": "boolean"}}}


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
    """A ``ClientOps`` over stand-in tools; ``ran`` lists each run's arguments."""

    def __init__(self) -> None:
        self.ran: list[dict[str, Any]] = []
        self.clock = _Clock()

        async def run(call: Call) -> dict[str, Any]:
            if call.arguments.get("n") == 13:
                raise OpError(ErrorCode.CONFLICT, "unlucky")
            if call.arguments.get("n") == 99:
                raise ConnectionError("database is down")
            self.ran.append(dict(call.arguments))
            if call.arguments.get("note"):
                call.turn.blocks.append("configuration shown")
            return {"task_id": "task-000000000001", "n": call.arguments.get("n"),
                    "shown_to_user": "Looper adds it to your reply"}

        async def escalate(call: Call) -> str | None:
            return f"delete {call.arguments.get('n')}?" if call.arguments.get("big") else None

        self.ops = ClientOps(
            Platform(bus=AsyncMock(), task_router=SimpleNamespace(has_planner=lambda _: False)),
            tools=(Tool("t_read", "reads", _SCHEMA, Risk.READ, run),
                   Tool("t_write", "writes", _SCHEMA, Risk.WRITE, run, escalate=escalate,
                        task_id=lambda arguments: "task-000000000009")),
            clock=self.clock)

    def call(self, name: str, arguments: Any = None, *, who: Principal = ALICE, session: str = "s1") -> dict[str, Any]:
        return asyncio.run(self.ops.call(who, session, name, {} if arguments is None else arguments))

    def confirm(self, confirmation_id: str, *, who: Principal = ALICE, session: str = "s1") -> dict[str, Any]:
        return asyncio.run(self.ops.confirm(who, session, confirmation_id))

    def ask(self, **kwargs: Any) -> str:
        answer = self.call("t_write", {"n": 5, "big": True}, **kwargs)
        assert answer["status"] == "needs_confirmation", answer
        return answer["confirmation_id"]


def _code(answer: dict[str, Any]) -> str:
    assert answer["status"] == "error", answer
    return answer["error"]["code"]


def test_the_clients_tools_are_the_managers_but_two_plus_their_own() -> None:
    manager = [t.name for t in MANAGER_TOOLS]
    client = [t.name for t in CLIENT_TOOLS]
    assert client == [n for n in manager if n not in ("looper_task_gate_reply", "looper_confirm")] + [
        "looper_task_pipeline", "looper_gates"]
    assert "looper_task_pipeline" not in manager + [t.name for t in TASKS_SCOPE_TOOLS]
    assert "looper_gates" not in manager + [t.name for t in TASKS_SCOPE_TOOLS]


def test_the_managers_catalog_does_not_import_the_clients() -> None:
    code = ("import sys, looper.agents.ops.service as s; "
            "print('looper.agents.ops.clients' in sys.modules, [t.name for t in s.MANAGER_TOOLS][-1])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["False", "looper_confirm"]


def test_a_read_runs_and_is_not_recorded(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    assert ops.call("t_read", {"n": 1}) == {"status": "ok", "result": {"task_id": "task-000000000001", "n": 1}}
    assert ops.ran == [{"n": 1}] and recorded == []


def test_a_write_runs_at_once_and_is_recorded_for_its_client(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    assert ops.call("t_write", {"n": 2})["status"] == "ok"
    assert ops.ran == [{"n": 2}]
    (row,) = recorded
    assert (row["channel"], row["chat"], row["tool"], row["risk"], row["status"], row["client_id"],
            row["manager_id"], row["task_id"]) == (API_CHANNEL, "s1", "t_write", "write", "ok", "client-aaa",
                                                   "manager", "task-000000000009")
    assert row["message_id"].startswith("api-")


def test_the_operator_is_recorded_without_a_client(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    assert ops.call("t_write", {"n": 2}, who=OPERATOR)["status"] == "ok"
    assert recorded[0]["client_id"] is None


def test_what_the_tool_adds_to_the_turn_is_the_answers_notes(recorded: list[dict[str, Any]]) -> None:
    answer = _Ops().call("t_write", {"n": 2, "note": True})
    assert answer["notes"] == ["configuration shown"]
    assert "notes" not in _Ops().call("t_write", {"n": 2})


def test_shown_to_user_is_left_out_of_a_result() -> None:
    assert "shown_to_user" not in _Ops().call("t_read", {"n": 1})["result"]


def test_errors_are_answered_and_recorded_but_a_reads_is_not(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    assert _code(ops.call("t_write", {"n": 13})) == "conflict"
    answer = ops.call("t_write", {"n": 99})
    assert _code(answer) == "unavailable" and "ConnectionError" in answer["error"]["message"]
    assert _code(ops.call("t_read", {"n": 13})) == "conflict"
    assert [(r["status"], r["error_code"]) for r in recorded] == [("error", "conflict"), ("unavailable", "unavailable")]


def test_a_high_call_is_asked_not_run_and_the_same_client_and_session_confirms_it_once(
    recorded: list[dict[str, Any]],
) -> None:
    ops = _Ops()
    answer = ops.call("t_write", {"n": 5, "big": True})
    assert answer == {"status": "needs_confirmation", "confirmation_id": answer["confirmation_id"],
                      "expires_in_seconds": CONFIRMATION_SECONDS, "impact": "delete 5?"}
    assert ops.ran == []
    done = ops.confirm(answer["confirmation_id"])
    assert done["status"] == "ok" and ops.ran == [{"n": 5, "big": True}]
    assert [(r["status"], r["risk"], r["confirmation_id"], r["client_id"]) for r in recorded] == [
        ("needs_confirmation", "high", answer["confirmation_id"], "client-aaa"),
        ("ok", "high", answer["confirmation_id"], "client-aaa")]
    assert _code(ops.confirm(answer["confirmation_id"])) == "not_found"
    assert ops.ran == [{"n": 5, "big": True}]


@pytest.mark.parametrize(("who", "session"), [(BOB, "s1"), (OPERATOR, "s1"), (ALICE, "s2")],
                         ids=["other-client", "operator", "other-session"])
def test_only_the_asking_client_in_the_asking_session_confirms(who: Principal, session: str) -> None:
    ops = _Ops()
    confirmation = ops.ask()
    assert _code(ops.confirm(confirmation, who=who, session=session)) == "not_found"
    assert ops.ran == []
    assert ops.confirm(confirmation)["status"] == "ok"
    assert ops.ran == [{"n": 5, "big": True}]


def test_a_newer_high_call_in_the_same_key_replaces_the_older() -> None:
    ops = _Ops()
    older, newer = ops.ask(), ops.ask()
    assert older != newer
    assert _code(ops.confirm(older)) == "not_found"
    assert ops.confirm(newer)["status"] == "ok"


def test_the_oldest_keys_confirmation_is_dropped_past_the_limit() -> None:
    ops = _Ops()
    first = ops.ask(session="k0")
    for i in range(1, CONFIRMATION_CHATS + 1):
        ops.ask(session=f"k{i}")
    assert _code(ops.confirm(first, session="k0")) == "not_found"


def test_a_confirmation_lapses() -> None:
    ops = _Ops()
    confirmation = ops.ask()
    ops.clock.now += CONFIRMATION_SECONDS + 1
    assert _code(ops.confirm(confirmation)) == "expired"
    assert ops.ran == []
    assert _code(ops.confirm(confirmation)) == "not_found"


def test_a_confirmed_call_that_fails_is_recorded_as_an_error(recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    answer = ops.call("t_write", {"n": 13, "big": True})
    assert _code(ops.confirm(answer["confirmation_id"])) == "conflict"
    assert [(r["status"], r["confirmation_id"]) for r in recorded] == [
        ("needs_confirmation", answer["confirmation_id"]), ("error", answer["confirmation_id"])]


@pytest.mark.parametrize(("name", "arguments", "session", "code"), [
    ("looper_task_gate_reply", {}, "s1", "forbidden"),
    ("looper_confirm", {"confirmation_id": "c-1"}, "s1", "forbidden"),
    ("t_nothing", {}, "s1", "invalid"),
    ("t_write", {}, "a b", "invalid"),
    ("t_write", {}, "x" * 65, "invalid"),
    ("t_write", {"api_key": "sk-x"}, "s1", "forbidden"),
    ("t_write", {"n": "1"}, "s1", "invalid"),
    ("t_write", [1], "s1", "invalid"),
])
def test_refusals(name: str, arguments: Any, session: str, code: str, recorded: list[dict[str, Any]]) -> None:
    ops = _Ops()
    assert _code(ops.call(name, arguments, session=session)) == code
    assert ops.ran == []


def test_the_manager_only_tools_are_refused_naming_the_replacement() -> None:
    message = _Ops().call("looper_task_gate_reply")["error"]["message"]
    assert "looper_gates" in message and "/api/v1/confirm" in message


def test_the_default_session_name_and_the_charset_are_accepted() -> None:
    ops = _Ops()
    for session in ("default", "a.b_c:d-e", "A" * 64):
        assert ops.call("t_read", {"n": 1}, session=session)["status"] == "ok"


def test_via_names_who_made_a_change() -> None:
    assert _via(Turn(channel=API_CHANNEL, chat="s", client_id="client-aaa", client_name="alice")) == "client:alice"
    assert _via(Turn(channel=API_CHANNEL, chat="s")) == "client:operator"
    assert _via(Turn(channel="web", chat="s", client_id="client-aaa")) == "manager:loopercli"
    assert _via(Turn(channel="web", chat="s")) == "manager:web"
    assert _via(Turn(channel=None, chat=None)) == "manager:unknown"
