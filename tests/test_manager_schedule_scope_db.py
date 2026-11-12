"""Task-scoped schedule permissions through ManagerOps and disposable PostgreSQL."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from typing import Any

import pytest

from looper.agents import schedules
from looper.agents.ops import Turn
from looper.db import schedules as db_schedules
from tests.db_support import DSN, run_db
from tests.test_manager_schedule_tools_db import _confirm, _run_of, _schedule
from tests.test_ops_tasks_db import _error, _project
from tests.test_scenario_manager_tools_db import REVIEW, _review

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
TOOL = "looper_schedules"
FIELDS = {"name": "progress", "cron": "*/30 * * * *", "requirement": "Report project progress.",
          "timezone": "UTC", "run_budget_usd": 1, "daily_budget_usd": 5}


def _ok(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["status"] == "ok", answer
    return answer["result"]


@pytest.mark.parametrize("scope", ["projects", "own"])
def test_task_manager_creates_disabled_and_confirms_in_next_message(monkeypatch, tmp_path, scope):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        chat, _ = await _review(pool, tmp_path, tools="tasks", task_scope=scope,
                                **({"scope_project_ids": [project["id"]]} if scope == "projects" else {}))
        assert TOOL in {tool["name"] for tool in await chat.ops.current_listing()}
        created, enable, run = await chat.say("Report every 30 minutes", (TOOL, {
            "action": "create", "project": "calc", "fields": FIELDS}),
            (TOOL, {"action": "enable", "schedule": "progress"}),
            (TOOL, {"action": "run", "schedule": "progress"}))
        sid = _ok(created)["schedule"]["schedule_id"]
        assert _error(enable) == _error(run) == "conflict"
        row = await db_schedules.get_schedule(sid)
        assert (row["manager_id"], row["created_via"], row["kind"], row["enabled"]) == (
            REVIEW, "manager", "agent", False)
        assert "Report project progress" in chat.turns[0].blocks[0]
        assert "要启用吗" in chat.turns[0].blocks[0]

        (asked,) = await chat.say("Enable it", (TOOL, {"action": "enable", "schedule": sid}))
        assert asked["status"] == "needs_confirmation"
        assert not (await db_schedules.get_schedule(sid))["enabled"]
        assert _ok(await _confirm(chat, asked))["schedule"]["enabled"]

        (asked,) = await chat.say("Increase cap", (TOOL, {
            "action": "update", "schedule": sid, "fields": {"daily_budget_usd": 6}}))
        assert asked["status"] == "needs_confirmation"
        assert (await db_schedules.get_schedule(sid))["daily_budget_usd"] == 5
        _ok(await _confirm(chat, asked))
        assert (await db_schedules.get_schedule(sid))["daily_budget_usd"] == 6

        from looper.agents.scheduler import Scheduler
        from looper.db import tasks

        scheduler = Scheduler(bus=chat.bus, runtime_root=tmp_path)
        chat.ops._platform = replace(chat.ops.platform, scheduler=scheduler)
        try:
            (asked,) = await chat.say("Run once", (TOOL, {"action": "run", "schedule": sid}))
            assert asked["status"] == "needs_confirmation"
            assert await pool.fetchval("SELECT count(*) FROM schedule_runs") == 0
            result = _ok(await _confirm(chat, asked))["run"]
            await scheduler.idle()
            run = await db_schedules.get_run(result["run_id"])
            task = await tasks.get_task(run["task_id"])
            assert (task["manager_id"], task["project_id"], task["schedule_id"]) == (REVIEW, project["id"], sid)
        finally:
            await scheduler.stop()

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("scope", ["projects", "own"])
def test_schedule_reads_and_writes_respect_project_or_owner(monkeypatch, tmp_path, scope):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        other = await _project(pool, "other", str(tmp_path))
        chat, default = await _review(pool, tmp_path, tools="tasks", task_scope=scope,
                                      **({"scope_project_ids": [project["id"]]} if scope == "projects" else {}))
        mine = await _schedule(project, "progress", manager_id=REVIEW)
        other_name = "Progress" if scope == "projects" else "Other progress"
        theirs = await _schedule(other if scope == "projects" else project, other_name)
        (listed,) = await chat.say("List", (TOOL, {"action": "list"}))
        assert [s["schedule_id"] for s in _ok(listed)["schedules"]] == [mine["schedule_id"]]
        # An inaccessible duplicate name does not make the visible name ambiguous.
        (got,) = await chat.say("Read", (TOOL, {"action": "get", "schedule": "progress"}))
        assert _ok(got)["schedule"]["schedule_id"] == mine["schedule_id"]
        for action in ("get", "update", "enable", "disable", "run", "delete", "reset_planner_session"):
            (refused,) = await chat.say("Change", (TOOL, {"action": action, "schedule": theirs["schedule_id"],
                **({"fields": {"name": "changed"}} if action == "update" else {})}))
            assert _error(refused) == "forbidden", (action, refused)
            assert not chat.turns[-1].blocks
        if scope == "projects":
            for action in ("list", "create"):
                (refused,) = await chat.say("Other project", (TOOL, {"action": action, "project": "other",
                    **({"fields": FIELDS} if action == "create" else {})}))
                assert _error(refused) == "forbidden"
        # General Manager retains access across projects and owners.
        (got,) = await default.say("Read", (TOOL, {"action": "get", "schedule": theirs["schedule_id"]}))
        assert _ok(got)["schedule"]["schedule_id"] == theirs["schedule_id"]
        assert (await db_schedules.get_schedule(theirs["schedule_id"]))["name"] == other_name

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("fields", [{"kind": "command"}, {"command": "exit 0"},
                                    {"env": {"MODE": "test"}}, {"env_file": "command.env"}])
def test_task_manager_refuses_command_fields_on_create_and_update(monkeypatch, tmp_path, fields):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        chat, _ = await _review(pool, tmp_path, tools="tasks", task_scope="projects",
                                scope_project_ids=[project["id"]])
        mine = await _schedule(project, "progress", manager_id=REVIEW)
        for action in ("create", "update"):
            arguments = {"action": action, "fields": {**FIELDS, **fields}}
            arguments.update({"project": "calc"} if action == "create" else {"schedule": mine["schedule_id"]})
            (refused,) = await chat.say("Command", (TOOL, arguments))
            assert _error(refused) == "forbidden", refused
        assert await pool.fetchval("SELECT count(*) FROM schedules") == 1
        assert (await db_schedules.get_schedule(mine["schedule_id"]))["kind"] == "agent"

    run_db(test, monkeypatch, timeout=30)


def test_project_command_schedules_are_read_only_and_all_manager_is_unchanged(monkeypatch, tmp_path):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        chat, default = await _review(pool, tmp_path, tools="tasks", task_scope="projects",
                                      scope_project_ids=[project["id"]])
        command = await _schedule(project, "command", kind="command", command="exit 0", requirement="")
        sid = command["schedule_id"]
        (listed,) = await chat.say("List", (TOOL, {"action": "list"}))
        assert [(s["schedule_id"], s["kind"]) for s in _ok(listed)["schedules"]] == [(sid, "command")]
        (got,) = await chat.say("Read", (TOOL, {"action": "get", "schedule": sid}))
        assert _ok(got)["schedule"]["command"] == "exit 0"
        for action in ("update", "enable", "disable", "run", "delete", "reset_planner_session"):
            (refused,) = await chat.say("Change", (TOOL, {"action": action, "schedule": sid,
                **({"fields": {"name": "changed"}} if action == "update" else {})}))
            assert _error(refused) == "forbidden", (action, refused)
            assert not chat.turns[-1].blocks
        assert (await db_schedules.get_schedule(sid))["name"] == "command"
        (asked,) = await default.say("Enable", (TOOL, {"action": "enable", "schedule": sid}))
        assert _ok(await _confirm(default, asked))["schedule"]["enabled"]
        (created,) = await default.say("Create command", (TOOL, {"action": "create", "project": "calc",
            "fields": {"name": "second-command", "cron": "*/30 * * * *", "kind": "command", "command": "exit 0"}}))
        assert _ok(created)["schedule"]["kind"] == "command"

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("empty", [False, True])
def test_confirmation_rechecks_schedule_project_after_scope_revision(monkeypatch, tmp_path, empty):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        other = await _project(pool, "other", str(tmp_path))
        chat, _ = await _review(pool, tmp_path, tools="tasks", task_scope="projects",
                                scope_project_ids=[project["id"]])
        sid = (await _schedule(project, "progress", manager_id=REVIEW))["schedule_id"]
        (asked,) = await chat.say("Enable", (TOOL, {"action": "enable", "schedule": sid}))
        assert asked["status"] == "needs_confirmation"
        await pool.execute("UPDATE managers SET scope_project_ids = $1, revision = revision + 1 WHERE id = $2",
                           [] if empty else [other["id"]], REVIEW)
        assert _error(await _confirm(chat, asked)) == "forbidden"
        assert not (await db_schedules.get_schedule(sid))["enabled"]

    run_db(test, monkeypatch, timeout=30)


def test_command_confirmation_is_refused_after_tools_are_narrowed(monkeypatch, tmp_path):
    async def test(pool):
        project = await _project(pool, work_dir=str(tmp_path))
        chat, _ = await _review(pool, tmp_path, tools="all", task_scope="own")
        sid = (await _schedule(project, "command", kind="command", command="exit 0", requirement="",
                               manager_id=REVIEW))["schedule_id"]
        (asked,) = await chat.say("Enable", (TOOL, {"action": "enable", "schedule": sid}))
        assert asked["status"] == "needs_confirmation"
        await pool.execute("UPDATE managers SET tools = 'tasks', revision = revision + 1 WHERE id = $1", REVIEW)
        assert _error(await _confirm(chat, asked)) == "forbidden"
        assert not (await db_schedules.get_schedule(sid))["enabled"]

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("channel,sender", [("lark", "ou_0123456789abcdef"), ("telegram", "12345")])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("tools", ["tasks", "all"])
def test_scoped_chat_schedule_keeps_default_notification(monkeypatch, tmp_path, channel, sender, group, tools):
    async def test(pool):
        from looper.agents.serve import seed_channel_configs
        from looper.db import chat_bindings

        project = await _project(pool, work_dir=str(tmp_path))
        chat, _ = await _review(pool, tmp_path, tools=tools, task_scope="projects" if tools == "tasks" else "own",
                                **({"scope_project_ids": [project["id"]]} if tools == "tasks" else {}))
        await seed_channel_configs(pool)
        config = {"allowed_open_ids" if channel == "lark" else "allowed_user_ids": [sender]}
        await pool.execute("UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = $2",
                           json.dumps(config), f"channel-{channel}")
        native_chat = (("oc_project" if channel == "lark" else "-98765") if group
                       else "oc_direct" if channel == "lark" else sender)
        expected_chat = native_chat if tools == "tasks" and group else sender
        # Bind the chat to this Manager, as the channel router does before the turn reaches it.
        async with pool.acquire() as conn, conn.transaction():
            await chat_bindings.put_binding(f"channel-{channel}", native_chat, REVIEW, conn=conn)
        turn = Turn(channel=channel, channel_id=f"channel-{channel}", chat=native_chat,
                    sender=sender, message_id="schedule-request", group=group)
        chat.ops.begin_turn(turn)
        try:
            created = await chat.ops.call(TOOL, {"action": "create", "project": "calc",
                                               "fields": {**FIELDS, "notify": "always"}})
        finally:
            chat.ops.end_turn(turn)
        sid = _ok(created)["schedule"]["schedule_id"]
        view = (await schedules.get_schedule(sid))["schedule"]
        assert view["notify_target"] == {"channel": channel, "chat": expected_chat}
        assert view["manager_id"] == REVIEW
        from looper.agents.schedule_notify import Notifier

        sent = []

        async def send(channel_id, target, text):
            sent.append((channel_id, target))
            return True, None

        notifier = Notifier(sender=send, clock=lambda: datetime.now(timezone.utc))
        run_id = await _run_of(pool, sid, "completed", summary="Project progress report")
        await notifier.run_ended(run_id)
        assert sent == [(f"channel-{channel}", expected_chat)]
        (notice,) = await db_schedules.list_notifications(sid, limit=5)
        assert (notice["chat"], notice["status"]) == (expected_chat, "sent")

    run_db(test, monkeypatch, timeout=30)
