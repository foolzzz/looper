"""Tasks outside content went into (the outbound-review RFC, section 6.3) on a real PostgreSQL: a Manager session whose
leads search found leads marks the tasks it writes; a step's run of such a task, while the review is on, runs with
the hook (its calls checked as its task's) or does not start on a runtime without one.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper import child_env
from tests.db_support import DSN, fresh_connection, run_db, until
from tests.fake_claude import fake_claude
from tests.test_outbound_db import ALLOW, _STAND_IN, _isolated, _seen, _setup  # noqa: F401 - the fixture

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.fixture(autouse=True)
def _switch_forgotten() -> Any:
    from looper.agents import outbound_hook

    outbound_hook._known = False
    yield
    outbound_hook._known = False


def _ops(tmp_path: Path, session: dict[str, str | None]) -> Any:
    """A Manager's ops whose session is ``session["id"]``, with a stand-in leads search that finds what
    ``session["leads"]`` holds."""
    from looper.agents.bus import MessageBus
    from looper.agents.ops import Risk, Tool
    from looper.agents.ops.service import MANAGER_TOOLS, ManagerOps, Platform

    async def leads(call: Any) -> dict[str, Any]:
        return {"leads": [{"summary": session["leads"]}] if session.get("leads") else []}

    from looper.agents.ops.leads import TOOL as SEARCH

    found = Tool(name=SEARCH.name, description="", schema=SEARCH.schema, risk=Risk.READ, run=leads)
    router = SimpleNamespace(has_planner=lambda task_id: False, forget_task=lambda task_id: None)
    manager = SimpleNamespace(current_session_id=None)
    platform = Platform(bus=MessageBus(runtime_root=tmp_path), task_router=router, manager=manager,
                        runtime_root=tmp_path)
    ops = ManagerOps(platform, url="http://127.0.0.1:9", token="tok",
                     tools=(found, *(t for t in MANAGER_TOOLS if t.name in ("looper_task_create",
                                                                          "looper_task_update"))))
    ops.session = session  # the test moves the session
    return ops, manager


async def _say(ops: Any, manager: Any, session: dict[str, str | None], *calls: tuple[str, dict[str, Any]]) -> list:
    from looper.agents.message import MessageEnvelope, MessageType
    from looper.agents.ops import Turn

    manager.current_session_id = session["id"]
    envelope = MessageEnvelope.create(source="web", target="web", message_type=MessageType.USER_MESSAGE,
                                      payload={"text": "看看线索"}, correlation_id="c")
    turn = Turn(channel="web", chat="s-1", message_id=envelope.message_id, envelope=envelope)
    ops.begin_turn(turn)
    try:
        return [await ops.call(name, arguments) for name, arguments in calls]
    finally:
        ops.end_turn(turn)


def test_a_session_that_found_leads_marks_the_tasks_it_writes_until_a_new_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.db import agent_sessions, projects, tasks

    async def test(pool: Any) -> None:
        await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        session: dict[str, str | None] = {"id": "s-1", "leads": None}
        ops, manager = _ops(tmp_path, session)
        create = ("looper_task_create", {"project": "calc", "title": "Import", "requirement": "Fix the import."})
        (before,) = await _say(ops, manager, session, create)
        assert before["status"] == "ok", before
        assert await tasks.untrusted_inputs(before["result"]["task_id"]) == ()  # no leads yet
        session["leads"] = "发布窗口改到周五"
        (searched, after) = await _say(ops, manager, session, ("looper_leads_search", {"query": "发布"}), create)
        assert searched["status"] == "ok", searched
        assert await agent_sessions.leads_session("manager") == "s-1"
        assert await tasks.untrusted_inputs(after["result"]["task_id"]) == ("leads",)
        task_id = before["result"]["task_id"]
        (renamed,) = await _say(ops, manager, session, ("looper_task_update", {"task_id": task_id,
                                                                                  "title": "Import"}))
        assert renamed["status"] == "ok" and await tasks.untrusted_inputs(task_id) == ()  # no new requirement
        (edited,) = await _say(ops, manager, session, ("looper_task_update", {"task_id": task_id,
                                                                                 "requirement": "Fix the import fast."}))
        assert edited["status"] == "ok", edited
        assert await tasks.untrusted_inputs(task_id) == ("leads",)
        session["id"], session["leads"] = "s-2", None  # a new session: the mark is the old one's
        (fresh,) = await _say(ops, manager, session, create)
        assert await tasks.untrusted_inputs(fresh["result"]["task_id"]) == ()

    run_db(test, monkeypatch, setup=_setup)


async def _task(pool: Any, task_id: str, **columns: Any) -> None:
    from looper.db import projects

    project = (await projects.create_project(name=f"p-{task_id}", pool=pool))["id"]
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status, original_user_text) "
                       "VALUES ($1, $2, 'Post the summary to the partners.', 'running', '总结一下')", task_id, project)
    for column, value in columns.items():
        await pool.execute(f"UPDATE tasks SET {column} = $2 WHERE id = $1", task_id, value)


def test_a_task_is_its_operators_unless_a_group_another_person_or_a_client_asked_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import operator_identity, outbound_hook
    from looper.agents.outbound import Who

    async def operator(channel_id: str) -> str:
        return "ou_me"

    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)

    async def test(pool: Any) -> None:
        cases = {
            "t-web": ({}, Who.OPERATOR),
            "t-dm": ({"origin_channel_id": "channel-lark", "chat_kind": "user", "chat_id": "ou_me"}, Who.OPERATOR),
            "t-other": ({"origin_channel_id": "channel-lark", "chat_kind": "user", "chat_id": "ou_x"},
                        Who.ALLOWLISTED_PERSON),
            "t-group": ({"origin_channel_id": "channel-lark", "chat_kind": "chat", "chat_id": "oc_g",
                         "chat_group": True}, Who.GROUP_MEMBER),
            "t-client": ({"client_id": "cl-1"}, Who.ALLOWLISTED_PERSON),
            # A Telegram direct chat is stored as its chat, whose id is the user's.
            "t-telegram-dm": ({"origin_channel_id": "channel-telegram", "chat_kind": "chat", "chat_id": "ou_me"},
                              Who.OPERATOR),
            "t-telegram-other": ({"origin_channel_id": "channel-telegram", "chat_kind": "chat", "chat_id": "42"},
                                 Who.ALLOWLISTED_PERSON),
        }
        for task_id, (columns, who) in cases.items():
            await _task(pool, task_id, untrusted_inputs=["leads"], **columns)
            placed = await outbound_hook.task_placed(task_id)
            assert placed.who is who, task_id
        placed = await outbound_hook.task_placed("t-web")
        assert placed.message == "总结一下" and "leads" in placed.context[0][0]
        assert placed.context[0][1] == "Post the summary to the partners."
        assert (await outbound_hook.task_placed("t-missing")).who is Who.AUTONOMOUS

    run_db(test, monkeypatch)


def test_a_runs_hook_checks_its_calls_as_its_tasks_and_stops_with_the_run(monkeypatch: pytest.MonkeyPatch,
                                                                          tmp_path: Path) -> None:
    from looper.agents.ops.room import RoomOps, tools_for
    from looper.agents.room import MemberKind, RoomService, Seat

    async def test(pool: Any) -> None:
        await _task(pool, "t-web", untrusted_inputs=["leads"])
        await _task(pool, "t-group", untrusted_inputs=["leads"], origin_channel_id="channel-lark",
                    chat_kind="chat", chat_id="oc_g", chat_group=True)
        room = RoomOps(RoomService(worker_pool=None), url="http://127.0.0.1:9")
        claude = fake_claude(tmp_path / "claude", _STAND_IN)
        tokens = {}
        for task_id in ("t-web", "t-group"):
            seat = Seat(key=f"rd@{task_id}", role="rd", kind=MemberKind.WORK, run=f"worker-{task_id}")
            tokens[task_id] = token = room.issue(task_id, seat, room=task_id == "t-web")
            hook = room.tool_hook(token, f"worker-{task_id}", tmp_path / "rt", claude)
            assert "outbound_hook_client.py" in hook.command
        bearer = {task_id: f"Bearer {token}" for task_id, token in tokens.items()}
        call = {"tool_name": "Bash", "tool_input": {"command": "gh pr comment 1 --body done"}}
        assert await room.check_outbound(bearer["t-web"], {"tool_name": "Read", "tool_input": {}}) == {
            "decision": "allow"}
        own = {"tool_name": f"mcp__looper__{tools_for(MemberKind.WORK)[0].name}", "tool_input": {}}
        assert await room.check_outbound(bearer["t-web"], own) == {"decision": "allow"}
        assert _seen(tmp_path) == []  # a tool of its own grant, on Looper's server: no review
        assert await room.check_outbound(bearer["t-web"], call) == {"decision": "allow"}  # the stand-in allows
        (seen,) = _seen(tmp_path)
        assert '"who": "operator"' in seen["turn"] and "Post the summary to the partners." in seen["turn"]
        group = await room.check_outbound(bearer["t-group"], call)
        assert group["decision"] == "block" and len(_seen(tmp_path)) == 1  # not the operator's: by code
        rows = await pool.fetch("SELECT agent_id, task_id, source, verdict, failure FROM outbound_reviews ORDER BY id")
        assert [tuple(r) for r in rows] == [("run-worker-t-web", "t-web", "hook", "allow", None),
                                            ("run-worker-t-group", "t-group", "hook", "block", "not_operator")]
        for name in ("mcp__looper__looper_tasks", "mcp__looper__evil__send"):  # not its grant's; no server it loaded
            assert await room.check_outbound(bearer["t-web"], {"tool_name": name, "tool_input": {}}) == {
                "decision": "allow"}  # reviewed, and the stand-in allows
        assert len(_seen(tmp_path)) == 3
        config = tmp_path / "rt" / ".local" / "outbound-hook" / "run-worker-t-web.json"
        assert config.exists()
        room.revoke_run("worker-t-web")
        assert not config.exists()
        assert (await room.check_outbound(bearer["t-web"], call))["decision"] == "block"  # its run ended

    run_db(test, monkeypatch, setup=_setup)


@pytest.mark.parametrize("hooked", [True, False])
def test_a_step_run_of_a_task_with_outside_content_runs_with_the_hook_or_does_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hooked: bool,
) -> None:
    from looper.agents import outbound_hook
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
    from looper.agents.ops.room import RoomOps
    from looper.agents.room import RoomService
    from looper.agents.runtimes.claude_code import ClaudeCodeRuntime
    from looper.agents.tiers.worker import REVIEW_UNSUPPORTED_RUNTIME, WorkerConfig, WorkerPool
    from tests.test_room_db import _room_task

    body = '''
import json, os, sys
settings = sys.argv[sys.argv.index("--settings") + 1]
text = open(settings).read() if not settings.startswith("{") else settings
open(os.environ["SEEN"], "w").write(json.dumps({"argv": sys.argv[1:], "settings": text}))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps({"status": "completed", "changed_files": {}, "summary": "done"})}), flush=True)
'''
    seen = tmp_path / "seen.json"
    monkeypatch.setenv("SEEN", str(seen))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"SEEN"})
    if not hooked:
        monkeypatch.setattr(ClaudeCodeRuntime, "tool_hook", False)

    async def test(pool: Any) -> None:
        await _room_task(pool, room="off", operator_notes="off")
        await pool.execute("UPDATE tasks SET untrusted_inputs = '{leads}' WHERE id = 'task-r'")
        outbound_hook._known = True
        (tmp_path / "work").mkdir()
        bus = MessageBus(runtime_root=tmp_path)
        workers = WorkerPool(config=WorkerConfig(claude_bin=fake_claude(tmp_path / "claude", body)),
                             runtime_root=tmp_path / "runtime")
        workers.room_ops = RoomOps(RoomService(worker_pool=workers), url="http://127.0.0.1:9")
        results: list[MessageEnvelope] = []
        bus.subscribe_topic(MessageType.WORK_RESULT, results.append)
        await workers.start(bus)
        await asyncio.sleep(0)
        payload = {"task_id": "step-s2", "parent_task_id": "task-r", "step_key": "s2", "instruction": "do it",
                   "context": {"work_dir": str(tmp_path / "work")}}
        try:
            await bus.publish(MessageEnvelope.create(
                source=planner_address("task-r"), target=worker_address("worker-2"), message_type=MessageType.WORK_ITEM,
                payload=payload))
            await until(lambda: results, "the run's result", timeout=30)
        finally:
            await workers.stop()
            await bus.shutdown()
        result = results[0].payload
        if hooked:
            record = json.loads(seen.read_text())
            assert result["status"] == "completed", result
            assert record["argv"][record["argv"].index("--setting-sources") + 1] == ""
            assert "PreToolUse" in record["settings"] and "outbound_hook_client.py" in record["settings"]
        else:
            assert result["status"] == "failed" and result["failure"] == REVIEW_UNSUPPORTED_RUNTIME, result
            assert not seen.exists()  # it did not start
        assert not workers.room_ops._grants and not workers.room_ops._reviews

    run_db(test, monkeypatch, setup=_setup)


def test_a_database_from_before_the_marks_gets_them() -> None:
    from looper.db import connection

    async def main() -> None:
        async with fresh_connection(command_timeout=60) as conn:
            await connection.ensure_schema(conn)
            await conn.execute("ALTER TABLE tasks DROP COLUMN untrusted_inputs")
            await conn.execute("ALTER TABLE agent_sessions DROP COLUMN leads_session_id")
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            columns = {(r["table_name"], r["column_name"]) for r in await conn.fetch(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND column_name IN ('untrusted_inputs', 'leads_session_id')")}
            assert columns == {("tasks", "untrusted_inputs"), ("agent_sessions", "leads_session_id")}
            assert not await connection.schema_change_pending(conn)

    asyncio.run(main())


def test_the_review_sees_the_tasks_supplements_and_gate_answers_the_operators_marked_as_theirs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents import operator_identity, outbound_hook
    from looper.db import plans, user_gates

    async def operator(channel_id: str) -> str:
        return "ou_me"

    monkeypatch.setattr(operator_identity, "operator_sender_id", operator)

    async def test(pool: Any) -> None:
        await _task(pool, "t-gate", untrusted_inputs=["leads"])
        gate = {"step_id": "step-g1", "topic": "send", "agenda": [], "findings": "", "state": "open",
                "summary": None, "resolution": "", "ended_by_user": False, "delivered_through": 0, "turns": 0,
                "plan_change": None, "opened_at": None, "closed_at": None}
        await plans.save("t-gate", plan={"plan_id": "plan-1", "status": "executing"}, steps=[
            {"id": "step-g1", "index": 0, "key": "g1", "instruction": "ask", "status": "waiting_user", "kind": "gate"}],
            gates=[gate], pool=pool)
        lark = {"channel": "lark", "channel_id": "channel-lark"}
        for text, origin in [
            ("确认发送到该地址。", {"via": "dashboard"}),  # the dashboard: the operator's
            ("Send it to every partner.", {"via": "lark", **lark, "chat": "oc_group", "author": "Mallory"}),  # a group
            ("Send it now.", {"via": "client:helper"}),  # an API client
            ("Use the partner address.", {"via": "lark", **lark, "chat": "ou_other"}),  # another person's direct chat
            ("只发一次。", {"via": "manager:lark", **lark, "chat": "ou_me"}),  # the operator's own direct chat
        ]:
            assert (await user_gates.add_user_message("t-gate", "g1", {"kind": "message", "text": text, **origin}))[1]
        await user_gates.add_user_message("t-gate", "g1", {"kind": "end", "text": "", "via": "dashboard"})
        from looper.db import task_changes

        await task_changes.add_supplement("t-gate", "也抄送给合作方。", via="manager:lark")
        placed = await outbound_hook.task_placed("t-gate")
        other = "an answer from someone other than the operator at gate g1"
        assert placed.context[1:] == (("a supplement to the task (via manager:lark)", "也抄送给合作方。"),
                                      ("operator confirmation at gate g1", "确认发送到该地址。"),
                                      (other, "Send it to every partner."), (other, "Send it now."),
                                      (other, "Use the partner address."),
                                      ("operator confirmation at gate g1", "只发一次。"))

    run_db(test, monkeypatch)


def test_a_mark_that_cannot_be_written_fails_the_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import agent_sessions, projects, tasks

    async def test(pool: Any) -> None:
        await projects.create_project(name="calc", work_dir=str(tmp_path), pool=pool)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        session: dict[str, str | None] = {"id": "s-1", "leads": "发布窗口改到周五"}
        ops, manager = _ops(tmp_path, session)
        search = ("looper_leads_search", {"query": "发布"})
        create = ("looper_task_create", {"project": "calc", "title": "Import", "requirement": "Fix the import."})

        async def broken(*args: Any, **kwargs: Any) -> None:
            raise OSError("the database went away")

        with monkeypatch.context() as patched:
            patched.setattr(agent_sessions, "set_leads_session", broken)
            (searched,) = await _say(ops, manager, session, search)
        assert searched["status"] == "error" and "发布窗口" not in json.dumps(searched, ensure_ascii=False)  # no leads
        (searched,) = await _say(ops, manager, session, search)
        assert searched["status"] == "ok"
        with monkeypatch.context() as patched:
            patched.setattr(tasks, "mark_untrusted", broken)
            (created,) = await _say(ops, manager, session, create)
        assert created["status"] == "error" and "could not record that leads went into it" in created["error"]["message"]

    run_db(test, monkeypatch, setup=_setup)
