"""Wait dashboard requests through the HTTP handler, with disposable PostgreSQL and no listener."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import waits
from looper.agents.http_server import AgentHTTPServer
from looper.agents.ops.clients import ClientOps
from looper.agents.ops.service import Platform
from looper.agents.message import MessageType, channel_address, planner_address
from looper.db import activity, plans, waits as db_waits
from tests.db_support import DSN, run_db
from tests.test_ops_tasks_db import _Chat, _project, _task
from tests.test_usage_api_db import _Writer
from tests.test_waits_db import NOW, SPEC, _plan_with_wait, _wait

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
TOKEN = "test-operator-token-which-is-never-written"


class Writer(_Writer):
    def get_extra_info(self, name: str) -> Any:
        return ("127.0.0.1", 12345) if name == "peername" else None

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class Bus:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def publish(self, message: Any) -> None:
        self.messages.append(message)


async def _api(method: str, path: str, body: bytes = b"", *, live: bool = False,
               auth: bool = True, origin: str | None = None, host: str = "127.0.0.1:9876") -> tuple[int, Any, Bus]:
    bus = Bus()
    server = AgentHTTPServer(port=9876, operator_token=TOKEN,
                             web_adapter=SimpleNamespace(_address=channel_address("web")),
                             supervisor=SimpleNamespace(bus=bus),
                             task_router=SimpleNamespace(has_planner=lambda _: live))
    server._client_ops = ClientOps(Platform(bus=bus, task_router=server._task_router))
    headers = [f"Host: {host}", f"Content-Length: {len(body)}"]
    if auth:
        headers.append(f"Authorization: Bearer {TOKEN}")
    if origin is not None:
        headers.append(f"Origin: {origin}")
    reader = asyncio.StreamReader()
    reader.feed_data((f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode() + body)
    reader.feed_eof()
    writer = Writer()
    await server._handle_connection(reader, writer)
    head, _, payload = writer.data.partition(b"\r\n\r\n")
    answer = json.loads(payload)
    if path.startswith("/api/v1/"):
        error = answer.get("error", "")
        answer = answer.get("result", {"error": error.get("message", "") if isinstance(error, dict) else error})
    return int(head.split()[1]), answer, bus


async def _open(pool: Any, **kwargs: Any) -> None:
    await _task(pool, "task-a", "waiting", await _project(pool))
    await _plan_with_wait(pool, state="waiting", opened_at=NOW, due_at=NOW + timedelta(seconds=60),
                          deadline_at=NOW + timedelta(hours=1), **kwargs)


def test_detail_waits_are_in_plan_order_with_all_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool: Any) -> None:
        check = {"type": "github_pr", "repo": "owner/name", "number": 12, "ready_when": "head_changed"}
        await _open(pool, spec={**SPEC, "check": check, "every_seconds": 600}, next_check_at=NOW)
        await db_waits.record_check("step-w1", at=NOW, result="error", output="pending", error="exit 1",
                                    next_check_at=NOW + timedelta(seconds=600))
        await pool.execute("UPDATE task_waits SET resolution = 'Keep waiting', notice = 'dashboard'")
        await plans.save("task-a", steps=[{"id": "step-w0", "index": 0, "key": "w0", "kind": "wait",
                                           "instruction": "earlier"}], waits=[_wait("step-w0")], pool=pool)
        status, detail, _ = await _api("GET", "/api/tasks/task-a")
        assert status == 200, detail
        before, row = detail["waits"]
        assert before == {"step": "w0", "step_id": "step-w0", "state": "pending", "outcome": None,
                          "topic": "CI finishes", "check": None, "every_seconds": None, "opened_at": None,
                          "due_at": None, "deadline_at": None, "next_check_at": None, "last_checked_at": None,
                          "checks": 0, "last_result": None, "last_output": "", "last_error": "",
                          "resolution": "", "notice": None, "closed_at": None, "depends_on": []}
        assert row == {"step": "w1", "step_id": "step-w1", "state": "waiting", "outcome": None,
                       "topic": "CI finishes", "check": check, "every_seconds": 600, "opened_at": NOW.isoformat(),
                       "due_at": (NOW + timedelta(seconds=60)).isoformat(),
                       "deadline_at": (NOW + timedelta(hours=1)).isoformat(),
                       "next_check_at": (NOW + timedelta(seconds=600)).isoformat(), "last_checked_at": NOW.isoformat(),
                       "checks": 1, "last_result": "error", "last_output": "pending", "last_error": "exit 1",
                       "resolution": "Keep waiting", "notice": "dashboard", "closed_at": None,
                       "depends_on": ["s1"]}

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("action", "live"), [("resume", True), ("cancel", False)])
def test_user_moves_wait_and_only_wakes_an_existing_planner(
    monkeypatch: pytest.MonkeyPatch, action: str, live: bool,
) -> None:
    async def test(pool: Any) -> None:
        await _open(pool)
        status, answer, bus = await _api("POST", f"/api/v1/waits/{action}",
            json.dumps({"task_id": "task-a", "step": "w1"}).encode(), live=live)
        assert status == 200, answer
        row = await db_waits.get("task-a", "w1")
        assert row is not None
        state, outcome, verb = ("ready", "resumed", "resumed") if action == "resume" else (
            "cancel_requested", None, "cancelled")
        assert (answer["task_id"], answer["wait"]["step"], answer["wait"]["depends_on"]) == (
            "task-a", "w1", ["s1"])
        assert (row["state"], row["outcome"], answer["wait"]["state"], answer["wait"]["outcome"]) == (
            state, outcome, state, outcome)
        assert row["resolution"] == f"{verb.capitalize()} by the user from the dashboard."
        (entry,) = await activity.list_activity("task-a")
        assert entry["summary"] == f"Wait w1 {verb} by the user"
        assert len(bus.messages) == int(live)
        if live:
            message = bus.messages[0]
            assert (message.source, message.target, message.message_type, message.payload) == (
                channel_address("web"), planner_address("task-a"), MessageType.WAIT_EVENT,
                {"task_id": "task-a", "wait": "w1"})
        status, _, bus = await _api("POST", f"/api/v1/waits/{action}", b'{"task_id":"task-a","step":"w1"}', live=True)
        assert status == 409 and not bus.messages
        assert len(await activity.list_activity("task-a")) == 1

    run_db(test, monkeypatch)


# Each way a move is refused once; a task that ended, or a wait that is not waiting, is refused alike by its status.
@pytest.mark.parametrize(("action", "case"), [
    ("resume", "no_task"), ("cancel", "no_label"), ("resume", "stopped"), ("cancel", "race"),
    *[(action, state) for action in ("resume", "cancel") for state in
      ("pending", "ready", "done", "timed_out", "cancel_requested", "failed", "skipped")],
])
def test_refused_moves_leave_no_activity_or_wakeup(
    monkeypatch: pytest.MonkeyPatch, action: str, case: str,
) -> None:
    async def test(pool: Any) -> None:
        await _open(pool)
        task_id, label = "task-a", "w1"
        expected = 409
        if case == "no_task":
            task_id, expected = "task-missing", 404
        elif case == "no_label":
            label, expected = "missing", 404
        elif case in ("completed", "cancelled", "task_failed", "stopped"):
            await pool.execute("UPDATE tasks SET status = $1", "failed" if case == "task_failed" else case)
        elif case == "race":
            move = db_waits.move

            async def competing(step_id: str, **kwargs: Any) -> Any:
                assert (await db_waits.get("task-a", "w1"))["state"] == "waiting"
                assert await move(step_id, to="timed_out") is not None
                return await move(step_id, **kwargs)

            monkeypatch.setattr(db_waits, "move", competing)
        else:
            await pool.execute("UPDATE task_waits SET state = $1, outcome = $2", case,
                               "ready" if case in ("ready", "done") else None)
        status, answer, bus = await _api("POST", f"/api/v1/waits/{action}",
            json.dumps({"task_id": task_id, "step": label}).encode(), live=True)
        assert status == expected, answer
        assert not bus.messages and not await activity.list_activity("task-a")
        if case in ("timed_out", "cancel_requested"):
            assert answer["error"] == f"wait w1 is {case}: its Planner decides what happens next"
        elif case in ("pending", "ready", "done", "failed", "skipped"):
            assert answer["error"] == f"wait w1 is {case}"
        if case == "race":
            assert (await db_waits.get("task-a", "w1"))["state"] == "timed_out"

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/api/v1/waits/resume"), ("GET", "/api/wait-settings"), ("PUT", "/api/wait-settings"),
])
def test_wait_routes_use_operator_auth_and_request_guard(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str,
) -> None:
    monkeypatch.setattr(waits, "_limits", waits.WaitLimits())
    monkeypatch.setattr(waits, "_sources", {})

    async def test(pool: Any) -> None:
        await _open(pool)
        for kwargs, expected in (({"auth": False}, 401), ({"origin": "https://other.example"}, 403),
                                 ({"host": "other.example"}, 403)):
            status, _, bus = await _api(method, path, b"{}", live=True, **kwargs)
            assert status == expected and not bus.messages
        assert (await db_waits.get("task-a", "w1"))["state"] == "waiting"
        assert not await activity.list_activity("task-a")
        # An authorized request reaches the same handler, rather than being refused unconditionally.
        status, _, _ = await _api(method, path, b'{"task_id":"task-a","step":"w1"}'
                                   if path.startswith("/api/v1/") else b'{"max_wait_seconds": 3600}')
        assert status == (404 if "/gates/" in path else 200)

    run_db(test, monkeypatch)


def test_settings_values_defaults_sources_reset_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waits, "_limits", waits.WaitLimits())
    monkeypatch.setattr(waits, "_sources", {})

    async def test(pool: Any) -> None:
        await waits.load()
        status, initial, _ = await _api("GET", "/api/wait-settings")
        assert status == 200
        assert initial == {
            "max_wait_seconds": {"value": waits.DEFAULT_MAX_WAIT_SECONDS, "default": waits.DEFAULT_MAX_WAIT_SECONDS,
                                 "min": 60, "max": 30 * 86400, "source": "built-in"},
            "min_check_seconds": {"value": 300, "default": 300, "min": 10, "max": 86400, "source": "built-in"}}
        status, changed, _ = await _api("PUT", "/api/wait-settings",
                                       b'{"max_wait_seconds": 600, "min_check_seconds": 60}')
        assert status == 200
        assert changed == {name: {**initial[name], "value": value, "source": "setting"}
                           for name, value in (("max_wait_seconds", 600), ("min_check_seconds", 60))}
        for body in (b"[]", b"invalid", b'{"unknown": 1}', b'{"max_wait_seconds": 59}',
                     b'{"max_wait_seconds": 60, "min_check_seconds": 61}'):
            status, answer, _ = await _api("PUT", "/api/wait-settings", body)
            assert status == 400 and answer["error"], (body, answer)
            assert (await _api("GET", "/api/wait-settings"))[1] == changed
        status, reset, _ = await _api("PUT", "/api/wait-settings",
                                     b'{"max_wait_seconds": null, "min_check_seconds": null}')
        assert status == 200 and reset == initial
        await waits.load()
        assert await waits.settings_view() == initial

    run_db(test, monkeypatch)


def test_manager_reads_changes_and_resets_wait_limits_only_after_confirmation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(waits, "_limits", waits.WaitLimits())
    monkeypatch.setattr(waits, "_sources", {})

    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        initial = (await _api("GET", "/api/wait-settings"))[1]
        (view,) = await chat.say("查看设置", ("looper_settings", {"action": "view"}))
        assert view["status"] == "ok", view
        assert view["result"]["waits"] == initial
        for values, expected in (({"max_wait_seconds": 600, "min_check_seconds": 60}, (600, 60)),
                                  ({"max_wait_seconds": None, "min_check_seconds": None},
                                   (waits.DEFAULT_MAX_WAIT_SECONDS, waits.DEFAULT_MIN_CHECK_SECONDS))):
            before = await waits.settings_view()
            (asked,) = await chat.say("修改等待设置", ("looper_settings", {"action": "waits", "waits": values}))
            assert asked["status"] == "needs_confirmation", asked
            assert await waits.settings_view() == before
            block = chat.turns[-1].blocks[0]
            assert "等待" in block and "→" in block
            for name, value in values.items():
                assert str(before[name]["value"]) in block
                assert str(before[name]["default"] if value is None else value) in block
            (answer,) = await chat.say("确认", ("looper_confirm", {"confirmation_id": asked["confirmation_id"]}))
            assert answer["status"] == "ok", answer
            assert {change["setting"] for change in answer["result"]["changes"]} == set(values)
            await waits.load()
            assert (waits.limits().max_wait_seconds, waits.limits().min_check_seconds) == expected
            assert (await _api("GET", "/api/wait-settings"))[1] == await waits.settings_view()

    run_db(test, monkeypatch)


# Which values are invalid is test_waits_protocol's; here the Manager's refusal leaves the stored limits alone.
@pytest.mark.parametrize("values", [{"max_wait_seconds": 59}])
def test_manager_refuses_invalid_wait_limits_before_asking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, values: dict[str, Any],
) -> None:
    monkeypatch.setattr(waits, "_limits", waits.WaitLimits())
    monkeypatch.setattr(waits, "_sources", {})

    async def test(pool: Any) -> None:
        chat = _Chat(tmp_path)
        before = await waits.settings_view()
        (answer,) = await chat.say("修改等待设置", ("looper_settings", {"action": "waits", "waits": values}))
        assert answer["status"] == "error" and answer["error"]["code"] == "invalid", answer
        assert not chat.turns[-1].blocks
        assert await waits.settings_view() == before

    run_db(test, monkeypatch)


def test_manager_task_detail_includes_wait_conditions_states_and_step_kinds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        check = {"type": "github_pr", "repo": "owner/name", "number": 12, "ready_when": "checks_finished"}
        await _open(pool, spec={**SPEC, "check": check, "every_seconds": 600}, next_check_at=NOW)
        await db_waits.record_check("step-w1", at=NOW, result="not_ready", output="pending", error="",
                                   next_check_at=NOW + timedelta(seconds=600))
        chat = _Chat(tmp_path)
        (answer,) = await chat.say("查看等待条件", ("looper_tasks", {"action": "get", "task_id": "task-a"}))
        assert answer["status"] == "ok", answer
        detail = answer["result"]
        assert [(step["step"], step["kind"]) for step in detail["steps"]] == [("s1", "work"), ("w1", "wait")]
        expected = (await _api("GET", "/api/tasks/task-a"))[1]["waits"]
        assert detail["waits"] == expected
        assert detail["waits"][0]["check"] == check
        assert detail["waits"][0]["state"] == "waiting"

    run_db(test, monkeypatch)
