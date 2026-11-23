"""The status card against PostgreSQL: empty reads, a populated card, a failed read that discards a good half,
and ``status_card.get`` while the switch is off and on.

Each test uses its own schema. The card stays off unless a test turns it on, and the header clock is Shanghai.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import chat_settings, status_card
from looper.agents.status_card import SessionActivity
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

NOW = datetime(2026, 11, 22, 7, 5, tzinfo=timezone.utc)
FIRE = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _card_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_card, "_enabled", False)
    monkeypatch.setattr(chat_settings, "_timezone", "Asia/Shanghai")


def _idle() -> SessionActivity:
    return SessionActivity("main", "idle", None, None, None, False, None, None, 0)


class _Fleet:
    """One idle Manager and no ``ops``, so confirmations stay unknown."""

    def agents(self) -> list[Any]:
        return [SimpleNamespace(name="Manager", agent_id="manager", activity=lambda: [_idle()])]


def _section(card: dict[str, Any], name: str) -> dict[str, Any]:
    found = [section for section in card["sections"] if section["name"] == name]
    assert len(found) == 1
    return found[0]


async def _schedule(pool: Any, project_id: str, name: str) -> str:
    from looper.db import schedules

    return await schedules.create_schedule(pool, project_id=project_id, profiles={}, fields={
        "name": name, "cron": "0 16 * * *", "timezone": "Asia/Shanghai", "requirement": name})


def test_an_empty_database_reads_as_empty_and_writes_no_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects as db_projects
    from looper.db import tasks as db_tasks

    async def test(pool: Any) -> None:
        async def unread(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("an empty task list must not read names")

        saved = (db_tasks.names_and_projects_of, db_projects.names_of)
        db_tasks.names_and_projects_of = unread
        db_projects.names_of = unread
        try:
            card = await status_card.snapshot(
                fleet=_Fleet(), task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
        finally:
            db_tasks.names_and_projects_of, db_projects.names_of = saved
        text = card["text"]
        assert text.startswith("截至 11-22 15:05")
        assert "管家：空闲；排队 0 条" in text
        assert "任务：没有在跑的" in text
        assert "定时任务：没有待发的" in text
        assert "等你" not in text and "提醒" not in text
        assert "recent" not in card and "lanes" not in card
        assert _section(card, "tasks")["count"] == 0
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                   status_card.ENABLED_KEY) == 0

    run_db(test, monkeypatch)


def test_stored_rows_fill_the_card_and_a_running_run_keeps_its_own_shape(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects, reminders, schedules, system_settings, tasks

    async def test(pool: Any) -> None:
        web = await projects.create_project(name="web", pool=pool)
        docs = await projects.create_project(name="docs", pool=pool)
        await tasks.create_task(task_id="t-login", requirement="cached login", project_id=web["id"],
                                title="修登录", status="running", pool=pool)
        await tasks.create_task(task_id="t-report", requirement="cached report", project_id=docs["id"],
                                title="整理周报", status="running", pool=pool)
        daily = await _schedule(pool, web["id"], "日报")
        patrol = await _schedule(pool, docs["id"], "巡检")
        await schedules.update_schedule(pool, daily, {"enabled": True, "next_fire_at": FIRE})
        await pool.execute("UPDATE schedules SET consecutive_failures = 1 WHERE id = $1", daily)
        await schedules.insert_run(pool, schedule_id=patrol, scheduled_for=NOW, trigger="scheduled",
                                   status="running", fired_at=NOW)
        await reminders.create(text="看 PR", due_at=DUE, timezone="Asia/Shanghai", channel_id="channel-telegram",
                               chat="7", created_channel="web", created_message_id=None, db=pool)
        await system_settings.put_settings({"attention.enabled": "true", "reminders.enabled": "true"}, pool=pool)

        card = await status_card.snapshot(
            fleet=_Fleet(),
            task_router=SimpleNamespace(running_planners=lambda: [
                {"task_id": "t-login", "name": "缓存登录", "project_id": "missing"},
                {"task_id": "t-report", "name": "缓存周报", "project_id": "missing"},
            ]),
            worker_pool=SimpleNamespace(runs=lambda: [
                {"task_id": "t-login", "role": "worker", "state": "running"},
                {"task_id": "t-login", "role": "worker", "state": "running"},
                {"task_id": "t-report", "role": "reviewer", "state": "running"},
            ]),
            now=NOW)
        text = card["text"]
        assert "截至 11-22 15:05" in text
        assert "修登录（web，Worker 2 个在跑）" in text
        assert "整理周报（docs，等评审）" in text
        assert text.index("修登录") < text.index("整理周报")
        assert "缓存登录" not in text and "缓存周报" not in text
        assert "正在跑 巡检" in text and "16:00 日报" in text
        assert "18:00 看 PR" in text
        assert "失败的定时任务" in text
        assert "登录这里读不到" in text and "确认这里读不到" in text
        assert "recent" not in card and "lanes" not in card
        shown = _section(card, "tasks")["shown"]
        assert [row["name"] for row in shown] == ["修登录", "整理周报"]
        assert _section(card, "schedules")["running"][0]["name"] == "巡检"
        assert _section(card, "reminders")["next"]["text"] == "看 PR"
        named = await schedules.named_running()
        assert any(row["name"] == "巡检" for row in named)
        running = await schedules.running_runs()
        assert running and "name" not in running[0]
        assert await pool.fetchval("SELECT count(*) FROM system_settings WHERE key = $1",
                                   status_card.ENABLED_KEY) == 0

    run_db(test, monkeypatch)


def test_a_failed_read_discards_the_half_that_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.db import projects, schedules
    from looper.db import tasks as db_tasks

    async def test(pool: Any) -> None:
        web = await projects.create_project(name="web", pool=pool)
        await tasks_create(pool, web["id"])
        daily = await _schedule(pool, web["id"], "日报")
        await schedules.update_schedule(pool, daily, {"enabled": True, "next_fire_at": FIRE})
        overview = await schedules.overview(NOW)
        assert any(row["name"] == "日报" for row in overview["upcoming"])

        async def boom(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("read failed")

        saved = (schedules.named_running, db_tasks.names_and_projects_of)
        schedules.named_running = boom
        db_tasks.names_and_projects_of = boom
        try:
            card = await status_card.snapshot(
                fleet=_Fleet(),
                task_router=SimpleNamespace(running_planners=lambda: [
                    {"task_id": "t-one", "name": "缓存任务", "project_id": web["id"]}]),
                worker_pool=None, now=NOW)
        finally:
            schedules.named_running, db_tasks.names_and_projects_of = saved
        text = card["text"]
        assert "没能读取" in text
        assert "日报" not in text
        assert "没有待发的" not in text and "没有在跑的" not in text
        assert _section(card, "tasks")["state"] == "error"
        assert _section(card, "schedules")["state"] == "error"

    async def tasks_create(pool: Any, project_id: str) -> None:
        from looper.db import tasks

        await tasks.create_task(task_id="t-one", requirement="one", project_id=project_id, title="一件事",
                                status="running", pool=pool)

    run_db(test, monkeypatch)


def test_get_conflicts_while_off_and_returns_the_card_once_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents.ops import ErrorCode, OpError
    from looper.agents.v1_domains import status_card as domain

    async def test(pool: Any) -> None:
        operation = SimpleNamespace(operation_id="status_card.get")
        server = SimpleNamespace(_managers=None, _task_router=None, _worker_pool=None)
        with pytest.raises(OpError) as caught:
            await domain.run(server, operation, {}, None, {})
        assert caught.value.code == ErrorCode.CONFLICT
        assert await status_card.update(True) == {"enabled": True}
        card = await domain.run(server, operation, {}, None, {})
        assert card["text"].startswith("截至 ")
        assert "recent" not in card and "lanes" not in card
        assert await pool.fetchval("SELECT value FROM system_settings WHERE key = $1",
                                   status_card.ENABLED_KEY) == "true"

    run_db(test, monkeypatch)


def test_the_oldest_pending_intent_is_not_taken_from_the_capped_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty shown intents are the urgent one and the newest ordinary ones. The card's earliest is the oldest."""
    from looper.agents import attention

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO event_sources (id, kind, name) VALUES ('s', 'lark_user', 'S')")
        await pool.executemany(
            "INSERT INTO system_settings (key, value) VALUES ($1, 'true')",
            [("attention.enabled",), ("intents.enabled",)])
        opened = datetime(2026, 11, 1, tzinfo=timezone.utc)
        oldest = None
        for index in range(attention.SHOWN + 1):
            intent_id = await pool.fetchval(
                "INSERT INTO intents (source_id, type_key, type_version, title, confidence, state, chat_id, "
                "created_at) VALUES ('s', 'dev_task', 1, $1, 'high', 'pending', 'chat', $2) RETURNING id",
                f"task {index:02d}", opened + timedelta(minutes=index))
            if index == 0:
                oldest = intent_id
        urgent = await pool.fetchval(
            "INSERT INTO intents (source_id, type_key, type_version, title, confidence, state, chat_id, created_at) "
            "VALUES ('s', 'alert_triage', 1, 'newer urgent', 'high', 'pending', 'chat', $1) RETURNING id",
            opened + timedelta(days=1))
        summary = await attention.collect()
        intents = next(section for section in summary["sections"] if section["kind"] == "intents")
        shown = [item["key"] for item in intents["items"]]
        assert intents["count"] == attention.SHOWN + 2
        assert shown[0] == f"intent:{urgent}" and f"intent:{oldest}" not in shown and len(shown) == attention.SHOWN

        card = await status_card.snapshot(
            fleet=_Fleet(), task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
        waiting = _section(card, "waiting")
        kind = next(item for item in waiting["kinds"] if item["kind"] == "intents")
        assert kind["count"] == attention.SHOWN + 2
        assert kind["earliest"]["key"] == f"intent:{oldest}"
        assert kind["earliest"]["title"] == "task 00"

    run_db(test, monkeypatch)
