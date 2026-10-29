"""Schedule ownership is immutable and checked again whenever a run can create a task."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from looper.agents import managers_api, schedules
from looper.agents.projects import RequestError
from looper.db import managers, schedules as db_schedules, tasks
from tests.db_support import DSN
from tests.schedule_support import (
    TELEGRAM_CHAT, ScheduleEnv, allow_telegram, run_scheduled, with_sender,
)

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_omitting_owner_uses_default_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        schedule = await env.schedule()
        assert schedule["manager_id"] == "manager" and schedule["manager_name"] == "Manager"
        run = (await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": schedule["schedule_id"]}))["run"]
        assert run["manager_id"] == "manager"
        assert (await tasks.get_task(run["task_id"]))["manager_id"] == "manager"
        await env.scheduler.idle()

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("deleted", [False, True])
def test_unknown_or_deleted_owner_cannot_save(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                             deleted: bool) -> None:
    async def test(env: ScheduleEnv) -> None:
        if deleted:
            await env.pool.execute("INSERT INTO managers (id, name, deleted_at, enabled, system_prompt) "
                                   "VALUES ('manager-review', 'Review', now(), false, 'Review alerts')")
        status, answer = await env.call("POST", "/api/v1/schedule_admin/create", {"project_id": env.project_id,
            "fields": {"name": "Review", "cron": "*/5 * * * *", "requirement": "Inspect alerts",
                       "manager_id": "manager-review"}})
        assert status == 400 and "deleted" in answer["error"]["message"]
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0

    run_scheduled(test, monkeypatch, tmp_path)


def test_disabled_owner_can_save_but_cannot_enable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, enabled, system_prompt) VALUES ('manager-review', 'Review', false, 'Review alerts')")
        schedule = await env.schedule(manager_id="manager-review")
        assert not schedule["enabled"] and schedule["manager_name"] == "Review"
        status, answer = await env.call("POST", "/api/v1/schedule_admin/update", {
            "schedule_id": schedule["schedule_id"], "fields": {"enabled": True}})
        assert status == 409 and "disabled" in answer["error"]["message"]
        assert not await env.pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", schedule["schedule_id"])

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("fire", ["manual", "due"])
@pytest.mark.parametrize("unavailable", ["disabled", "deleted", "templates_only"])
def test_unavailable_owner_fails_firing_without_task_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                           fire: str, unavailable: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        with_sender(env)
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.enabled(manager_id="manager-review", task_limits={"worker": 2},
                                 profile_overrides={"worker": "agent-claude-code-opus"}))["schedule_id"]
        if unavailable == "templates_only":
            await env.pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager-review'")
        elif unavailable == "deleted":
            await env.pool.execute("UPDATE managers SET enabled = false, deleted_at = now() WHERE id = 'manager-review'")
        else:
            await env.pool.execute("UPDATE managers SET enabled = false WHERE id = 'manager-review'")
        if fire == "manual":
            await env.scheduler.run_now(sid)
        else:
            await env.due(sid)
            await env.scheduler.tick()
        await env.scheduler.idle()
        run = (await env.runs(sid))[0]
        expected = "requires a template" if unavailable == "templates_only" else unavailable
        assert run["status"] == "failed" and run["task_id"] is None and expected in run["error"]
        assert run["manager_id"] == "manager-review"
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM task_profile_overrides") == 0
        assert await env.pool.fetchval("SELECT count(*) FROM task_concurrency_limits") == 0
        assert env.bus.of("task_start") == []

    run_scheduled(test, monkeypatch, tmp_path)


def test_templates_only_owner_requires_a_template_when_saving(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, task_policy, system_prompt) "
                               "VALUES ('manager-review', 'Review', 'templates_only', 'Review alerts')")
        with pytest.raises(RequestError, match="requires a template"):
            await schedules.create_schedule(env.project_id, {"name": "Review", "cron": "*/5 * * * *",
                "requirement": "Inspect alerts", "manager_id": "manager-review"})
        assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0

    run_scheduled(test, monkeypatch, tmp_path)


def test_policy_change_prevents_enabling_free_form_schedule(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.schedule(manager_id="manager-review"))["schedule_id"]
        await env.pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager-review'")
        with pytest.raises(RequestError, match="requires a template"):
            await schedules.update_schedule(sid, {"enabled": True})
        assert not await env.pool.fetchval("SELECT enabled FROM schedules WHERE id = $1", sid)

    run_scheduled(test, monkeypatch, tmp_path)


def test_templates_only_policy_refuses_free_form_edits_but_allows_disabling(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) "
                               "VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.enabled(manager_id="manager-review"))["schedule_id"]
        await env.pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager-review'")
        assert not (await schedules.update_schedule(sid, {"enabled": False}))["schedule"]["enabled"]
        with pytest.raises(RequestError, match="requires a template"):
            await schedules.update_schedule(sid, {"requirement": "Changed free-form task"})
        assert await env.pool.fetchval("SELECT requirement FROM schedules WHERE id = $1", sid) == "Look at the new alerts."

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("enabled", [False, True])
def test_owner_patch_is_refused_even_for_disabled_schedules(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path, enabled: bool) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        schedule = await (env.enabled() if enabled else env.schedule())
        with pytest.raises(RequestError, match="owner cannot change"):
            await schedules.update_schedule(schedule["schedule_id"], {"manager_id": "manager-review"})
        assert await env.pool.fetchval("SELECT manager_id FROM schedules WHERE id = $1", schedule["schedule_id"]) == "manager"

    run_scheduled(test, monkeypatch, tmp_path)


def test_schedule_reference_blocks_manager_deletion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        await env.schedule(manager_id="manager-review")
        assert await managers.delete_manager("manager-review") == "has_schedules"
        with pytest.raises(RequestError, match="delete those schedules first") as error:
            await managers_api.delete_manager("manager-review")
        assert error.value.status == "409 Conflict"

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("first", ["create", "delete"])
def test_delete_create_race_preserves_owner_reference(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                     first: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        async with env.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            try:
                if first == "create":
                    await db_schedules.create_schedule(conn, project_id=env.project_id,
                        fields={"name": "Review", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Inspect"},
                        profiles={}, manager_id="manager-review")
                    pending = asyncio.create_task(managers.delete_manager("manager-review"))
                else:
                    await conn.fetchrow("SELECT id FROM managers WHERE id = 'manager-review' FOR UPDATE")
                    await conn.execute("UPDATE managers SET enabled = false, deleted_at = now() WHERE id = 'manager-review'")
                    pending = asyncio.create_task(schedules.create_schedule(env.project_id,
                        {"name": "Review", "cron": "*/5 * * * *", "requirement": "Inspect", "manager_id": "manager-review"}))
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending), 0.1)
                await tx.commit()
            except BaseException:
                await tx.rollback()
                raise
        if first == "create":
            assert await asyncio.wait_for(pending, 5) == "has_schedules"
            assert await env.pool.fetchval("SELECT count(*) FROM schedules WHERE manager_id = 'manager-review'") == 1
        else:
            with pytest.raises(RequestError, match="deleted"):
                await asyncio.wait_for(pending, 5)
            assert await env.pool.fetchval("SELECT count(*) FROM schedules") == 0

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("fire", ["manual", "due", "queued", "gated"])
def test_every_task_launch_keeps_schedule_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fire: str) -> None:
    from looper.agents import scheduler
    from looper.agents.schedule_gate import GateResult

    async def test(env: ScheduleEnv) -> None:
        with_sender(env)
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        monkeypatch.setattr(scheduler, "run_gate", AsyncMock(return_value=GateResult(0, "new alert", "")))
        sid = (await env.enabled(manager_id="manager-review", overlap="queue",
                                gate_command="echo new" if fire == "gated" else None))["schedule_id"]
        if fire in ("due", "gated"):
            await env.due(sid)
            await env.scheduler.tick()
        else:
            await env.scheduler.run_now(sid)
        await env.scheduler.idle()
        run = (await env.runs(sid))[0]
        if fire == "queued":
            queued = await env.scheduler.run_now(sid)
            assert (await db_schedules.get_run(queued))["status"] == "queued"
            await env.end_task(run["task_id"], "completed")
            await env.scheduler.idle()
            run = next(row for row in await env.runs(sid) if row["run_id"] == queued)
        task = await tasks.get_task(run["task_id"])
        assert run["manager_id"] == task["manager_id"] == "manager-review"
        assert task["origin_channel_id"] == "" and task["origin_thread"] is None
        assert task["chat_id"] is None and task["chat_kind"] is None
        last = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]["last_run"]
        assert last["manager_id"] == "manager-review"

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("branch", ["queued", "gated"])
@pytest.mark.parametrize("unavailable", ["disabled", "templates_only"])
def test_owner_unavailable_while_waiting_prevents_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                        branch: str, unavailable: str) -> None:
    from looper.agents import scheduler
    from looper.agents.schedule_gate import GateResult

    async def test(env: ScheduleEnv) -> None:
        with_sender(env)
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        started, release = asyncio.Event(), asyncio.Event()

        async def gate(*args: Any, **kwargs: Any) -> GateResult:
            started.set()
            await release.wait()
            return GateResult(0, "new alert", "")

        monkeypatch.setattr(scheduler, "run_gate", gate)
        sid = (await env.enabled(manager_id="manager-review", overlap="queue",
                                gate_command="echo new" if branch == "gated" else None))["schedule_id"]
        first = await env.scheduler.run_now(sid)
        pending_run = first
        if branch == "gated":
            await asyncio.wait_for(started.wait(), 5)
        else:
            first_task = (await db_schedules.get_run(first))["task_id"]
            pending_run = await env.scheduler.run_now(sid)
        if unavailable == "templates_only":
            await env.pool.execute("UPDATE managers SET task_policy = 'templates_only' WHERE id = 'manager-review'")
        else:
            await env.pool.execute("UPDATE managers SET enabled = false WHERE id = 'manager-review'")
        if branch == "gated":
            release.set()
        else:
            await env.end_task(first_task, "completed")
        await env.scheduler.idle()
        run = next(row for row in await env.runs(sid) if row["run_id"] == pending_run)
        assert run["manager_id"] == "manager-review" and run["status"] == "failed" and run["task_id"] is None
        assert ("requires a template" if unavailable == "templates_only" else "disabled") in run["error"]
        assert await env.pool.fetchval("SELECT count(*) FROM tasks") == (1 if branch == "queued" else 0)

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("status", ["skipped_gate", "gate_failed", "skipped_budget", "skipped_overlap"])
def test_no_task_run_readback_has_owner_without_manager_turns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                             status: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        with_sender(env)
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.schedule(manager_id="manager-review"))["schedule_id"]
        run_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock(), trigger="scheduled",
                                               status=status, finished_at=env.clock())
        assert (await db_schedules.get_run(run_id))["manager_id"] == "manager-review"
        assert (await env.runs(sid))[0]["manager_id"] == "manager-review"
        await env.scheduler._notifier.run_ended(run_id)
        await env.scheduler.idle()
        assert not env.bus.sent

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("available", [True, False])
def test_task_reports_route_to_owner_with_existing_disabled_fallback(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path, available: bool) -> None:
    from looper.agents.bus import MessageBus
    from looper.agents.message import TASK_ROUTER_ADDRESS, MessageEnvelope, MessageType
    from looper.agents.tiers.task import Task, TaskRouter

    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.schedule(manager_id="manager-review"))["schedule_id"]
        run_id = await env.scheduler.run_now(sid)
        task_id = (await db_schedules.get_run(run_id))["task_id"]
        task = Task.from_row(task_id, await tasks.get_task(task_id))
        router = TaskRouter(runtime_root=tmp_path)
        bus = MessageBus(runtime_root=tmp_path)
        router._bus, router._tasks[task_id] = bus, task
        router.manager_running = lambda mid: mid == "manager" or available
        if not available:
            await env.pool.execute("UPDATE managers SET enabled = false WHERE id = 'manager-review'")
        for kind, status in ((MessageType.TASK_ACCEPTED, "accepted"), (MessageType.GOAL_STATUS, "executing"),
                             (MessageType.GOAL_STATUS, "completed")):
            envelope = MessageEnvelope.create(source=TASK_ROUTER_ADDRESS, target=TASK_ROUTER_ADDRESS,
                message_type=kind, payload={"task_id": task_id, "status": status, "summary": "Synthetic report"})
            if kind == MessageType.TASK_ACCEPTED:
                await router._handle_task_accepted(envelope)
            else:
                await router._handle_planner_status(envelope)
        target = "manager-review" if available else "manager"
        assert bus.get_queue(target).qsize() == 3
        assert bus.get_queue("manager" if available else "manager-review").qsize() == 0
        assert (await tasks.get_task(task_id))["manager_id"] == "manager-review"
        await env.scheduler.idle()

    run_scheduled(test, monkeypatch, tmp_path)


def test_native_notifications_keep_builtin_target_and_use_sender_fake(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sender = with_sender(env)
        await allow_telegram(env)
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.schedule(manager_id="manager-review", notify="always",
                                  notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT}))["schedule_id"]
        run_id = await env.scheduler.run_now(sid)
        await env.end_task((await db_schedules.get_run(run_id))["task_id"], "completed", summary="Synthetic result")
        await env.scheduler.idle()
        assert len(sender.sent) == 1 and sender.sent[0][:2] == ("channel-telegram", TELEGRAM_CHAT)
        assert (await tasks.get_task((await db_schedules.get_run(run_id))["task_id"]))["origin_channel_id"] == ""

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("owner", ["manager", "manager-review"])
def test_manager_schedule_tool_assigns_call_owner_without_changing_compact_results(monkeypatch: pytest.MonkeyPatch,
                                                                                  tmp_path: Path, owner: str) -> None:
    from looper.agents.ops.service import DEFAULT_SCOPE
    from tests.test_ops_tasks_db import _Chat

    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt, tools) "
                               "VALUES ('manager-review', 'Review', 'Review alerts', 'all')")
        chat = _Chat(tmp_path)
        chat.ops.set_scope(replace(DEFAULT_SCOPE, manager_id=owner))
        (answer,) = await chat.say("Create schedule", ("looper_schedules", {"action": "create", "project": "alerts",
            "fields": {"name": "Review", "cron": "*/5 * * * *", "requirement": "Inspect alerts"}}))
        assert answer["status"] == "ok", answer
        sid = answer["result"]["schedule"]["schedule_id"]
        assert await env.pool.fetchval("SELECT manager_id FROM schedules WHERE id = $1", sid) == owner
        assert "manager_id" not in answer["result"]["schedule"]
        (got,) = await chat.say("Read schedule", ("looper_schedules", {"action": "get", "schedule": sid}))
        assert got["status"] == "ok", got
        assert not {"manager_id", "manager_name"} & got["result"]["schedule"].keys()

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("change", ["enabled = false", "task_policy = 'templates_only'"])
def test_owner_change_waits_until_firing_task_insert_commits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                           change: str) -> None:
    async def test(env: ScheduleEnv) -> None:
        await env.pool.execute("INSERT INTO managers (id, name, system_prompt) "
                               "VALUES ('manager-review', 'Review', 'Review alerts')")
        sid = (await env.schedule(manager_id="manager-review"))["schedule_id"]
        inserting, release = asyncio.Event(), asyncio.Event()
        create_task = tasks.create_task

        async def held_insert(**kwargs: Any) -> dict[str, Any]:
            inserting.set()
            await release.wait()
            return await create_task(**kwargs)

        monkeypatch.setattr(tasks, "create_task", held_insert)
        firing = asyncio.create_task(env.scheduler.run_now(sid))
        await asyncio.wait_for(inserting.wait(), 5)
        updating = asyncio.create_task(env.pool.execute(f"UPDATE managers SET {change} WHERE id = 'manager-review'"))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(updating), 0.1)
        finally:
            release.set()
        run_id = await asyncio.wait_for(firing, 5)
        await asyncio.wait_for(updating, 5)
        run = await db_schedules.get_run(run_id)
        assert run["status"] == "running"
        assert (await tasks.get_task(run["task_id"]))["manager_id"] == "manager-review"
        await env.scheduler.idle()

    run_scheduled(test, monkeypatch, tmp_path)
