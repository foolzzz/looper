"""Scheduled user gates use the schedule's current notify target and existing gate API and courier."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.db_support import DSN, run_db
from tests.test_gate_channels_db import _Poster, _task_with_gate
from tests.test_user_gates_db import _api

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("destination", ["schedule", "dashboard", "page", "removed"])
def test_scheduled_gates_deliver_to_the_schedule_target_then_the_dashboard_target(
    monkeypatch: pytest.MonkeyPatch, destination: str,
) -> None:
    async def test(pool: Any) -> None:
        from looper.agents.gate_commands import _waiting
        from looper.agents.gate_courier import GateCourier
        from looper.agents.task_notice import NotifyTarget, TargetKind
        from looper.db import projects, schedules, tasks, user_gates

        # These are stand-in allowlist ids; the poster records messages locally.
        await pool.execute("INSERT INTO channel_configs (channel_id, channel_type, enabled, config) VALUES "
                           "('channel-telegram', 'telegram', true, $1::jsonb) ON CONFLICT (channel_id) DO UPDATE "
                           "SET config = EXCLUDED.config, enabled = true",
                           json.dumps({"allowed_user_ids": ["4242", "5151"]}))
        if destination != "page":
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, $2)",
                               "user_gates.dashboard_target", json.dumps({"channel": "telegram", "chat": "5151"}))
        await _task_with_gate(pool)
        task = await tasks.get_task("task-a")
        async with pool.acquire() as conn:
            sid = await schedules.create_schedule(conn, project_id=task["project_id"], fields={
                "name": "check", "cron": "*/5 * * * *", "timezone": "UTC", "requirement": "Check updates",
                "user_gates": "on", **({"notify_channel": "telegram", "notify_chat": "4242"}
                                        if destination in ("schedule", "removed") else {}),
            }, profiles={})
        await pool.execute("UPDATE tasks SET schedule_id = $1 WHERE id = 'task-a'", sid)
        project = await projects.get_task_project("task-a")
        assert project["task_schedule_id"] == sid and project["task_schedule_user_gates"] == "on"
        if destination == "removed":
            await pool.execute("UPDATE channel_configs SET config = $1::jsonb WHERE channel_id = 'channel-telegram'",
                               json.dumps({"allowed_user_ids": ["5151"]}))
        poster = _Poster()
        assert await GateCourier(poster).run_once() == 1
        status, detail, _ = await _api("POST", "/api/v1/tasks/detail", {"task_id": "task-a"})
        assert status == 200
        if destination in ("page", "removed"):
            assert poster.posts == [] and detail["gates"][0]["channel_posts"] is None
        else:
            target = "4242" if destination == "schedule" else "5151"
            assert len(poster.posts) == 1
            assert poster.posts[0]["target"] == NotifyTarget(TargetKind.CHAT, target)
            assert detail["gates"][0]["channel_posts"]["sent"] == 1
            assert [gate["task_id"] for gate in await _waiting("channel-telegram", target, True, 1, None)] == ["task-a"]
            other = "5151" if target == "4242" else "4242"
            assert await _waiting("channel-telegram", other, True, 1, None) == []

        status, answer, bus = await _api("POST", "/api/v1/ops/gates/reply", {
            "task_id": "task-a", "gate": "g1", "text": "PostgreSQL", "agenda_item": "q1", "option": "PostgreSQL",
        }, live=True)
        assert status == 200 and answer["gate"]["state"] == "discussing"
        assert any(message.message_type.value == "gate_message" for message in bus.published)
        assert (await user_gates.pending("task-a"))[0]["text"] == "PostgreSQL"
        # Deleting a schedule clears the task's link, making its gate policy attended again.
        await pool.execute("DELETE FROM schedules WHERE id = $1", sid)
        assert (await projects.get_task_project("task-a"))["task_schedule_id"] is None

    run_db(test, monkeypatch, timeout=20)


def test_a_waiting_user_gate_keeps_overlap_skip_and_its_inherited_run_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from datetime import datetime, timedelta

    from tests.schedule_support import run_scheduled

    async def test(env: Any) -> None:
        await env.ok("POST", "/api/v1/scheduler_settings/update", {"run_timeout_seconds": 600})
        sid = (await env.enabled(user_gates="on", overlap="skip"))["schedule_id"]
        await env.due(sid)
        await env.scheduler.tick()
        (run,) = await env.runs(sid)
        task_id = run["task_id"]
        assert await env.pool.fetchval("SELECT run_timeout_seconds FROM schedule_runs WHERE id = $1",
                                      run["run_id"]) == 600
        await env.pool.execute("UPDATE tasks SET status = 'waiting_user' WHERE id = $1", task_id)
        await env.due(sid)
        await env.scheduler.tick()
        assert (await env.runs(sid))[0]["status"] == "skipped_overlap"
        assert env.bus.of("task_start") == [task_id]
        # A later system default does not extend the timeout the waiting run started with.
        await env.ok("POST", "/api/v1/scheduler_settings/update", {"run_timeout_seconds": 3600})
        env.clock.now = datetime.fromisoformat(run["fired_at"]) + timedelta(seconds=600)
        await env.scheduler.tick()
        stored = await env.pool.fetchrow("SELECT status, error FROM schedule_runs WHERE id = $1", run["run_id"])
        assert stored["status"] == "timed_out" and "600 s" in stored["error"]
        assert env.bus.of("task_stop") == [task_id]

    run_scheduled(test, monkeypatch, tmp_path)
