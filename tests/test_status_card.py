"""The status card snapshot: typed sections, code-rendered text, and the off-path prompt and tool catalog.

The switch is off unless a test turns it on. Readers that would touch PostgreSQL are quiet unless a test
replaces them. The launched-process comparison lives in ``test_status_command``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import chat_commands, chat_settings, status_card
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.session_routing import SessionKey, SessionKind
from looper.agents.status_card import SessionActivity
from looper.agents.tiers.manager_session import MAIN_SESSION, ManagerSession

NOW = datetime(2026, 11, 22, 7, 5, tzinfo=timezone.utc)
FIRE = datetime(2026, 11, 22, 8, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
EXAMPLE = (
    "截至 11-22 15:05\n"
    "管家：在回答你（控制台，3 分钟）；排队 1 条\n"
    "任务：2 个在跑：修登录（web，Worker 2 个在跑）、整理周报（docs，等评审）\n"
    "定时任务：下一个 16:00 日报\n"
    "等你：失败的定时任务 1 · 意图 2 · 待办 1（发 /todo 看详情）\n"
    "提醒：下一条 18:00 看 PR"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def test_waiting_card_names_blocked_vault_commits():
    text = status_card._waiting_text({"kinds": [
        {"kind": "vault_commits", "state": "ok", "count": 1}]})
    assert "知识库提交受阻 1" in text and "/todo" in text


def _tools_sha(tools: Any) -> str:
    payload = [{"name": tool.name, "description": tool.description, "schema": tool.schema} for tool in tools]
    return _sha(json.dumps(payload, sort_keys=True, ensure_ascii=False))


class _Agent:
    def __init__(self, activity: list[SessionActivity], *, name: str = "Manager", agent_id: str = "manager") -> None:
        self.name, self.agent_id, self._activity = name, agent_id, activity

    def activity(self) -> list[SessionActivity]:
        return self._activity


class _Fleet:
    def __init__(self, *agents: _Agent) -> None:
        self._agents = list(agents)

    def agents(self) -> list[_Agent]:
        return self._agents


def _idle(**kwargs: Any) -> SessionActivity:
    fields = dict(key=MAIN_SESSION, state="idle", origin=None, channel=None, chat=None, group=False,
                  client_id=None, started_at=None, queued=0)
    fields.update(kwargs)
    return SessionActivity(**fields)


def _answering(**kwargs: Any) -> SessionActivity:
    fields = dict(key=MAIN_SESSION, state="answering", origin="user", channel="web", chat=None, group=False,
                  client_id=None, started_at=NOW - timedelta(minutes=3), queued=1)
    fields.update(kwargs)
    return SessionActivity(**fields)


@pytest.fixture(autouse=True)
def _quiet_readers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off, and no section reaches PostgreSQL unless the test replaces that reader."""
    monkeypatch.setattr(status_card, "_enabled", False)
    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    monkeypatch.setattr(chat_settings, "_timezone", None)

    async def overview(since: datetime, **_: Any) -> dict[str, Any]:
        return {"upcoming": []}

    async def named(**_: Any) -> list[Any]:
        return []

    async def names(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    async def off(**_: Any) -> bool:
        return False

    monkeypatch.setattr("looper.db.schedules.overview", overview)
    monkeypatch.setattr("looper.db.schedules.named_running", named)
    monkeypatch.setattr("looper.db.tasks.names_and_projects_of", names)
    monkeypatch.setattr("looper.db.projects.names_of", names)
    monkeypatch.setattr("looper.agents.attention.enabled", off)
    monkeypatch.setattr("looper.agents.card_actions.enabled", off)
    monkeypatch.setattr("looper.agents.reminders.enabled", off)


def test_prompts_tools_and_commands_match_the_catalog_without_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """Frozen while the card is off: the prompts, the tool catalogs, /help and the command list."""
    from looper.agents.ops.planner import TOOLS as PLANNER_TOOLS
    from looper.agents.ops.service import MANAGER_TOOLS
    from looper.agents.ops.settings import TOOLS as SETTINGS_TOOLS
    from looper.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT as MANAGER_PROMPT
    from looper.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT as PLANNER_PROMPT
    from looper.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT as WORKER_PROMPT

    monkeypatch.setattr(chat_settings, "_stop_enabled", False)
    assert _sha(MANAGER_PROMPT) == "5872b0ea1d2e1cff"
    assert _sha(PLANNER_PROMPT) == "35b66a88bdf0b2c3"
    assert _sha(WORKER_PROMPT) == "78e7f6473c3f1424"
    assert _tools_sha(MANAGER_TOOLS) == "15f3a88ebc096521"
    assert _tools_sha(PLANNER_TOOLS) == "96b7f999bcbcfb54"
    assert _sha(repr(chat_commands.COMMANDS)) == "07ac7d7d40b5a07c"
    assert _sha(chat_commands.help_text()) == "6194cfaf71e1b330"
    assert [row for row in chat_commands.COMMANDS if row[0] == "status"] == [("status", "查看当前任务进度")]
    schema = next(tool.schema for tool in SETTINGS_TOOLS if tool.name == "looper_settings")
    assert "status_card" not in json.dumps(schema)
    assert all("status_card" not in json.dumps(tool.schema) for tool in MANAGER_TOOLS)


def test_the_example_snapshot_is_the_card_text(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def names(ids: Any, **_: Any) -> dict[str, Any]:
        assert set(ids) == {"t-login", "t-report"}
        return {"t-login": {"name": "修登录", "project_id": "p-web"},
                "t-report": {"name": "整理周报", "project_id": "p-docs"}}

    async def projects(ids: Any, **_: Any) -> dict[str, str]:
        assert set(ids) == {"p-web", "p-docs"}
        return {"p-web": "web", "p-docs": "docs"}

    async def overview(since: datetime, **_: Any) -> dict[str, Any]:
        assert since == NOW
        return {"upcoming": [{"id": "sched-daily", "name": "日报", "project_name": "web",
                              "next_fire_at": FIRE, "timezone": "Asia/Shanghai"}]}

    async def collect(**_: Any) -> dict[str, Any]:
        return {"sections": [
            {"kind": "schedules", "state": "ok", "count": 1, "items": [
                {"key": "schedule:sched-daily", "title": "日报", "since": NOW}], "error": None},
            {"kind": "intents", "state": "ok", "count": 2, "items": [], "error": None},
            {"kind": "ledger", "state": "ok", "count": 1, "items": [], "error": None},
        ]}

    async def on(**_: Any) -> bool:
        return True

    async def pending(**_: Any) -> dict[str, Any]:
        return {"due_at": DUE, "text": "看 PR", "timezone": "Asia/Shanghai"}

    monkeypatch.setattr(chat_settings, "_timezone", "Asia/Shanghai")
    monkeypatch.setattr("looper.db.tasks.names_and_projects_of", names)
    monkeypatch.setattr("looper.db.projects.names_of", projects)
    monkeypatch.setattr("looper.db.schedules.overview", overview)
    monkeypatch.setattr("looper.db.schedules.named_running", _async([]))
    monkeypatch.setattr("looper.agents.attention.enabled", on)
    monkeypatch.setattr("looper.agents.attention.collect", collect)
    monkeypatch.setattr("looper.agents.reminders.enabled", on)
    monkeypatch.setattr("looper.db.reminders.next_pending", pending)
    router = SimpleNamespace(running_planners=lambda: [
        {"task_id": "t-login", "name": "旧名字", "project_id": "p-old"},
        {"task_id": "t-report", "name": "缓存周报", "project_id": "p-docs"},
    ])
    pool = SimpleNamespace(runs=lambda: [
        {"task_id": "t-login", "role": "worker", "state": "running"},
        {"task_id": "t-login", "role": "worker", "state": "running"},
        {"task_id": "t-report", "role": "reviewer", "state": "running"},
    ])
    fleet = _Fleet(_Agent([_answering()]))

    async def check() -> None:
        with caplog.at_level(logging.INFO, logger="looper.agents.status_card"):
            card = await status_card.snapshot(fleet=fleet, task_router=router, worker_pool=pool, now=NOW)
        assert card["text"] == EXAMPLE
        assert card["read_at"] == NOW.isoformat()
        assert [section["name"] for section in card["sections"]] == [
            "manager", "tasks", "schedules", "waiting", "reminders"]
        assert "recent" not in card and "lanes" not in json.dumps(card)
        schedule = next(section for section in card["sections"] if section["name"] == "schedules")
        assert schedule["next"][0]["next_fire_at"] == FIRE.isoformat()
        assert schedule["next"][0]["schedule_id"] == "sched-daily"
        reminder = next(section for section in card["sections"] if section["name"] == "reminders")
        assert reminder["next"]["due_at"] == DUE.isoformat()
        assert "修登录" not in caplog.text and "看 PR" not in caplog.text
        assert "status_card source=snapshot elapsed_ms=" in caplog.text and "failed=-" in caplog.text

    asyncio.run(check())


def _async(value: Any) -> Any:
    async def inner(*_: Any, **__: Any) -> Any:
        return value
    return inner


def test_sections_say_what_they_could_not_read_and_leave_off_features_out(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    boom = RuntimeError("overview\ndown")

    async def fail_overview(*_: Any, **__: Any) -> Any:
        raise boom

    async def named(**_: Any) -> list[dict[str, str]]:
        return [{"id": "run-1", "schedule_id": "sched-1", "name": "日报", "project_name": "web",
                 "fired_at": FIRE, "timezone": "UTC"}]

    monkeypatch.setattr("looper.db.schedules.overview", fail_overview)
    monkeypatch.setattr("looper.db.schedules.named_running", named)

    async def check() -> None:
        with caplog.at_level(logging.INFO, logger="looper.agents.status_card"):
            card = await status_card.snapshot(
                fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
                worker_pool=None, now=NOW, source="get")
            assert "定时任务：没能读取（RuntimeError: overview down）" in card["text"]
            assert "日报" not in card["text"] and "没有待发的" not in card["text"]
            assert "任务：没有在跑的" in card["text"] and "等你" not in card["text"] and "提醒" not in card["text"]
            assert all(section["name"] != "schedules" or section["state"] == "error" for section in card["sections"])
            assert _source_line(caplog).endswith("failed=schedules")
            caplog.clear()

            huge = RuntimeError("x" * 500)
            monkeypatch.setattr("looper.db.schedules.overview", _raising(huge))
            card = await status_card.snapshot(
                fleet=None, task_router=SimpleNamespace(running_planners=lambda: (_ for _ in ()).throw(huge)),
                worker_pool=None, now=NOW)
            tasks = next(section for section in card["sections"] if section["name"] == "tasks")
            assert tasks["state"] == "error" and len(tasks["error"]) == 300 and "\n" not in tasks["error"]
            assert card["text"].startswith("截至 ")
            assert "管家：这里读不到" in card["text"] and "没有在跑的" not in card["text"]
            manager = card["sections"][0]
            assert manager == {"name": "manager", "state": "unknown", "error": None}
            assert _source_line(caplog).endswith("failed=tasks,schedules")

    asyncio.run(check())


def _source_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("status_card source=")]
    assert lines
    return lines[-1]


def _raising(exc: BaseException) -> Any:
    async def inner(*_: Any, **__: Any) -> Any:
        raise exc
    return inner


def test_a_reader_that_fails_does_not_blank_the_others(monkeypatch: pytest.MonkeyPatch,
                                                      caplog: pytest.LogCaptureFixture) -> None:
    async def check() -> None:
        with caplog.at_level(logging.INFO, logger="looper.agents.status_card"):
            card = await status_card.snapshot(
                fleet=_Fleet(_Agent([_idle(queued=2)])),
                task_router=SimpleNamespace(running_planners=lambda: (_ for _ in ()).throw(RuntimeError("tasks down"))),
                worker_pool=None, now=NOW)
        assert "管家：空闲；排队 2 条" in card["text"]
        assert "任务：没能读取（RuntimeError: tasks down）" in card["text"]
        assert "定时任务：没有待发的" in card["text"]
        assert "failed=tasks" in caplog.text

        class _Down:
            def agents(self) -> list[Any]:
                raise RuntimeError("fleet down")

        card = await status_card.snapshot(
            fleet=_Down(), task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
        assert card["text"].splitlines()[1] == "管家：没能读取（RuntimeError: fleet down）"
        assert "任务：没有在跑的" in card["text"]

    asyncio.run(check())


def test_empty_and_unknown_callers_do_not_invent_work(monkeypatch: pytest.MonkeyPatch) -> None:
    reads = {"tasks": 0, "projects": 0, "runs": 0}

    async def tasks(*_: Any, **__: Any) -> dict[str, Any]:
        reads["tasks"] += 1
        return {}

    async def projects(*_: Any, **__: Any) -> dict[str, Any]:
        reads["projects"] += 1
        return {}

    monkeypatch.setattr("looper.db.tasks.names_and_projects_of", tasks)
    monkeypatch.setattr("looper.db.projects.names_of", projects)

    async def check() -> None:
        card = await status_card.snapshot(
            fleet=_Fleet(), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "管家：没有在运行的" in card["text"]
        assert "任务：没有在跑的" in card["text"]
        assert reads == {"tasks": 0, "projects": 0, "runs": 0}
        pool = SimpleNamespace(runs=lambda: reads.__setitem__("runs", reads["runs"] + 1) or [])
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([])), task_router=None, worker_pool=pool, now=NOW)
        assert "任务：这里读不到" in card["text"] and reads["runs"] == 0
        naive = await status_card.snapshot(
            fleet=_Fleet(), task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None,
            now=NOW.replace(tzinfo=None))
        assert naive["text"].splitlines()[0].startswith("截至 ")
        assert naive["read_at"].endswith("+00:00")

    asyncio.run(check())


def test_task_lines_cap_sort_and_name_the_running_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = {
        "t-plan": {"name": "只规划", "project_id": "p-docs"},
        "t-wait": {"name": "等位置", "project_id": None},
        "t-both": {"name": "两边", "project_id": "p-web"},
        "t-rev": {"name": "两个评审", "project_id": "p-web"},
        "t-one": {"name": "等一个评审", "project_id": None},
        "t-plain": {"name": "只有名字", "project_id": None},
        "t-mark": {"name": "修\u200e登录", "project_id": "p-web"},
    }

    async def names(ids: Any, **_: Any) -> dict[str, Any]:
        return {task_id: rows[task_id] for task_id in ids}

    async def projects(ids: Any, **_: Any) -> dict[str, str]:
        return {project_id: "web" if project_id == "p-web" else "docs" for project_id in ids}

    monkeypatch.setattr("looper.db.tasks.names_and_projects_of", names)
    monkeypatch.setattr("looper.db.projects.names_of", projects)
    planners = [{"task_id": "t-plan", "name": "缓存名", "project_id": "p-docs"},
                {"task_id": "t-both", "name": "缓存", "project_id": "p-web"},
                {"task_id": "t-mark", "name": "缓存", "project_id": "p-web"}]
    runs = [
        {"task_id": "t-wait", "role": "worker", "state": "waiting"},
        {"task_id": "t-wait", "role": "reviewer", "state": "waiting"},
        {"task_id": "t-both", "role": "worker", "state": "running"},
        {"task_id": "t-both", "role": "worker", "state": "waiting"},
        {"task_id": "t-both", "role": "reviewer", "state": "running"},
        {"task_id": "t-rev", "role": "reviewer", "state": "running"},
        {"task_id": "t-rev", "role": "reviewer", "state": "running"},
        {"task_id": "t-one", "role": "reviewer", "state": "running"},
        {"task_id": "t-plain", "role": "other", "state": "running"},
        {"task_id": "t-mark", "role": "worker", "state": "running"},
    ]

    async def check() -> None:
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])),
            task_router=SimpleNamespace(running_planners=lambda: planners),
            worker_pool=SimpleNamespace(runs=lambda: runs), now=NOW)
        text = next(line for line in card["text"].splitlines() if line.startswith("任务："))
        assert text.startswith("任务：7 个在跑：") and "另有 2 个" in text
        assert "缓存名" not in text and "缓存" not in text and "\u200e" not in text
        shown = text.split("：", 2)[2].split("，另有")[0]
        assert shown.index("修登录") < shown.index("整理") if "整理" in shown else "修登录" in shown
        assert "只规划（docs，Planner 在跑）" in text
        assert "两边（web，Worker 1 个在跑，1 个等位置，Reviewer 1 个在跑）" in text
        assert "两个评审（web，Reviewer 2 个在跑）" in text
        assert "等一个评审（等评审）" in text
        assert "修登录（web，Worker 1 个在跑）" in text
        assert "只有名字" not in text and "等位置（" not in text
        tasks = next(section for section in card["sections"] if section["name"] == "tasks")
        assert tasks["count"] == 7 and tasks["more"] == 2 and len(tasks["shown"]) == 5
        assert [row["task_id"] for row in tasks["shown"]] == [
            "t-rev", "t-both", "t-mark", "t-plan", "t-one"]

    asyncio.run(check())


def test_schedule_lines_cap_at_three_and_a_bad_zone_stays_in_the_section(monkeypatch: pytest.MonkeyPatch) -> None:
    upcoming = [{"id": f"sched-{index}", "name": f"任务{index}", "project_name": "web",
                 "next_fire_at": FIRE + timedelta(days=index), "timezone": "Asia/Shanghai"} for index in range(5)]
    upcoming.append({"id": "sched-bad", "name": "坏时区", "project_name": "web", "next_fire_at": FIRE,
                     "timezone": "Not/AZone"})

    async def overview(*_: Any, **__: Any) -> dict[str, Any]:
        return {"upcoming": upcoming}

    async def named(**_: Any) -> list[dict[str, Any]]:
        return [{"id": "run-1", "schedule_id": "sched-0", "name": "巡检", "project_name": "web",
                 "fired_at": FIRE, "timezone": "Asia/Shanghai"}]

    monkeypatch.setattr(chat_settings, "_timezone", "Asia/Shanghai")
    monkeypatch.setattr("looper.db.schedules.overview", overview)
    monkeypatch.setattr("looper.db.schedules.named_running", named)

    async def check() -> None:
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        line = next(item for item in card["text"].splitlines() if item.startswith("定时任务："))
        assert line.startswith("定时任务：正在跑 巡检；下一个 ")
        assert "16:00 任务0" in line and "11-23 16:00 任务1" in line and "11-24 16:00 任务2" in line
        assert "任务3" not in line and "任务4" not in line and "坏时区" not in line
        schedules = next(section for section in card["sections"] if section["name"] == "schedules")
        assert schedules["state"] == "ok" and len(schedules["next"]) == 3
        assert schedules["running"] == [{"run_id": "run-1", "schedule_id": "sched-0", "name": "巡检",
                                         "project_name": "web", "fired_at": FIRE.isoformat(),
                                         "timezone": "Asia/Shanghai"}]

    asyncio.run(check())


def test_where_a_session_is_and_how_long_it_has_run() -> None:
    cases = [
        (dict(channel="web", client_id=None, group=False), "控制台"),
        (dict(channel="web", client_id="client-1"), "API 客户端"),
        (dict(channel="telegram", group=False), "Telegram"),
        (dict(channel="telegram", group=True), "Telegram 群"),
        (dict(channel="lark", group=False), "飞书"),
        (dict(channel="lark", group=True), "飞书群"),
        (dict(channel="lark_bot", group=True), "飞书群"),
        (dict(channel="sms"), "聊天"),
        (dict(channel=None), "聊天"),
        (dict(channel=""), "聊天"),
    ]
    durations = [(59, "不到 1 分钟"), (60, "1 分钟"), (3599, "59 分钟"), (3600, "1 小时"), (86400, "1 天")]

    async def check() -> None:
        for fields, label in cases:
            started = NOW - timedelta(seconds=180)
            card = await status_card.snapshot(
                fleet=_Fleet(_Agent([_answering(started_at=started, **fields)])),
                task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
            assert f"在回答你（{label}，3 分钟）" in card["text"]
        for seconds, label in durations:
            card = await status_card.snapshot(
                fleet=_Fleet(_Agent([_answering(started_at=NOW - timedelta(seconds=seconds))])),
                task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
            assert f"在回答你（控制台，{label}）" in card["text"]
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_answering(state="background", origin="completion")]),
                         _Agent([_answering(state="background", origin="own", key="other")],
                                name="第二位", agent_id="manager-2")),
            task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
        assert "在跑后台回合（收尾，3 分钟）" in card["text"]
        assert "第二位：在跑后台回合（自己发起，3 分钟）；排队 1 条" in card["text"]
        assert "管家：" not in card["text"]

    asyncio.run(check())


def test_waiting_counts_errors_and_the_earliest_item(monkeypatch: pytest.MonkeyPatch) -> None:
    early = NOW - timedelta(hours=2)
    late = NOW - timedelta(hours=1)

    async def collect(**_: Any) -> dict[str, Any]:
        # The shown list keeps only the later gate. The oldest one is a separate read.
        return {"sections": [
            {"kind": "gates", "state": "ok", "count": 1, "items": [
                {"key": "gate:late", "title": "晚的\n关卡", "since": late}], "error": None},
            {"kind": "confirmations", "state": "unknown", "count": 0, "items": [], "error": None},
            {"kind": "schedules", "state": "error", "count": 0, "items": [], "error": "down"},
            {"kind": "logins", "state": "ok", "count": 0, "items": [], "error": None},
        ]}

    async def on(**_: Any) -> bool:
        return True

    async def earliest(kind: str, **_: Any) -> dict[str, Any]:
        assert kind == "gates"
        return {"key": "gate:early", "title": "早的关卡", "since": early}

    monkeypatch.setattr("looper.agents.attention.enabled", on)
    monkeypatch.setattr("looper.agents.attention.collect", collect)
    monkeypatch.setattr("looper.agents.attention.earliest", earliest)

    async def check() -> None:
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "等你：关卡 1 · 确认这里读不到 · 失败的定时任务没能读取（发 /todo 看详情）" in card["text"]
        waiting = next(section for section in card["sections"] if section["name"] == "waiting")
        assert waiting["state"] == "ok"
        earliest = next(kind for kind in waiting["kinds"] if kind["kind"] == "gates")["earliest"]
        assert earliest == {"key": "gate:early", "title": "早的关卡", "since": early.isoformat()}

        async def empty(**_: Any) -> dict[str, Any]:
            return {"sections": [{"kind": "gates", "state": "ok", "count": 0, "items": [], "error": None}]}

        monkeypatch.setattr("looper.agents.attention.collect", empty)
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "等你：没有" in card["text"]

        async def down(**_: Any) -> dict[str, Any]:
            raise RuntimeError("collect down")

        monkeypatch.setattr("looper.agents.attention.collect", down)
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "等你：没能读取（RuntimeError: collect down）" in card["text"]

        async def unread(**_: Any) -> bool:
            raise RuntimeError("switch")

        monkeypatch.setattr("looper.agents.attention.enabled", unread)
        monkeypatch.setattr("looper.agents.reminders.enabled", unread)
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "等你：没能读取（RuntimeError: switch）" in card["text"]
        assert "提醒：没能读取（RuntimeError: switch）" in card["text"]

        async def off(**_: Any) -> bool:
            return False

        monkeypatch.setattr("looper.agents.attention.enabled", off)
        monkeypatch.setattr("looper.agents.reminders.enabled", off)
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "等你" not in card["text"] and "提醒" not in card["text"]

    asyncio.run(check())


def test_confirmations_follow_a_fleet_that_can_list_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fleet without ``ops`` is unknown; one confirmation on a fleet that has it is counted. Other kinds may fail."""
    from looper.agents import attention

    class _Ops:
        def waiting_confirmations(self) -> list[dict[str, Any]]:
            return [{"confirmation_id": "c-1", "impact": "发消息", "tool": "send", "waited_seconds": 10,
                     "chat": "1", "channel": "web", "expires_in_seconds": 60}]

    class _ConfirmFleet:
        def agents(self) -> list[Any]:
            return [_Agent([_idle()])]

        def ops(self, agent_id: str) -> _Ops:
            assert agent_id == "manager"
            return _Ops()

    async def on(**_: Any) -> bool:
        return True

    monkeypatch.setattr(attention, "enabled", on)

    async def check() -> None:
        card = await status_card.snapshot(
            fleet=_ConfirmFleet(), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "确认 1" in card["text"] and "等你：没能读取" not in card["text"]
        card = await status_card.snapshot(
            fleet=status_card.SoloFleet(_Agent([_idle()])),
            task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
        assert "确认这里读不到" in card["text"] and "等你：没能读取" not in card["text"]

    asyncio.run(check())


def test_reminders_name_the_next_one_or_say_there_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def on(**_: Any) -> bool:
        return True

    async def pending(**_: Any) -> dict[str, Any] | None:
        return {"due_at": DUE, "text": "看\nPR", "timezone": "Not/AZone"}

    monkeypatch.setattr(chat_settings, "_timezone", "Asia/Shanghai")
    monkeypatch.setattr("looper.agents.reminders.enabled", on)
    monkeypatch.setattr("looper.db.reminders.next_pending", pending)

    async def check() -> None:
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "提醒：下一条 10:00 看 PR" in card["text"]
        monkeypatch.setattr("looper.db.reminders.next_pending", _async(None))
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "提醒：没有待发的" in card["text"]
        monkeypatch.setattr("looper.db.reminders.next_pending", _raising(RuntimeError("reminder down")))
        card = await status_card.snapshot(
            fleet=_Fleet(_Agent([_idle()])), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "提醒：没能读取（RuntimeError: reminder down）" in card["text"]
        assert "没有待发的" not in card["text"].split("提醒：", 1)[1]

    asyncio.run(check())


def test_a_bad_header_zone_still_reads_the_card(monkeypatch: pytest.MonkeyPatch,
                                                caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setattr(chat_settings, "_timezone", "Not/AZone")

    async def check() -> None:
        with caplog.at_level(logging.WARNING, logger="looper.agents.status_card"):
            card = await status_card.snapshot(
                fleet=_Fleet(), task_router=SimpleNamespace(running_planners=lambda: []), worker_pool=None, now=NOW)
        assert card["text"].splitlines()[0].startswith("截至 ")
        assert "header timezone Not/AZone is not usable" in caplog.text

    asyncio.run(check())


def test_queue_accounting_folds_the_bus_into_main_only_while_routing_is_off(tmp_path: Any,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import lanes_settings, session_routing
    from looper.agents.managers_api import DEFAULT_MANAGER_ID
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
    from looper.agents.tiers.turn_token import TurnToken

    # Folding the bus into the main queue is the lanes-off path. A previous test on this
    # worker can leave the process-wide switch on; this case is about routing, not lanes.
    monkeypatch.setattr(lanes_settings, "_enabled", False)
    monkeypatch.setattr(session_routing, "_routing_enabled", False)

    async def check() -> None:
        manager = ManagerAgent(config=ManagerConfig(claude_bin="claude", work_dir=tmp_path), runtime_root=tmp_path)
        assert manager.agent_id == DEFAULT_MANAGER_ID
        key = SessionKey(SessionKind.DM, "channel-telegram", "7")
        main = manager._sessions[MAIN_SESSION]
        other = ManagerSession(key=key.text, route=key, claude=main.claude, claude_runtime=main.claude_runtime,
                               last_activity=0.0)
        manager._sessions[key.text] = other
        manager._router._queues[MAIN_SESSION] = asyncio.Queue()
        manager._router._queues[key.text] = asyncio.Queue()
        assert manager._router.queued(main) is False
        manager._router._queues[MAIN_SESSION].put_nowait(object())
        manager._router._queues[key.text].put_nowait(object())
        manager._router._queues[key.text].put_nowait(object())
        assert manager._router.queued(other) is True
        manager._bus = SimpleNamespace(pending_count=lambda agent_id: 1)
        manager._handling = SimpleNamespace(message_id="handling-1")

        def totals() -> tuple[int, int, str]:
            activity = status_card.session_activity(manager)
            assert [item.key for item in activity] == [MAIN_SESSION, key.text]
            text = "\n".join(_manager_line(activity))
            return activity[0].queued, activity[1].queued, text

        assert totals() == (3, 2, "空闲 5")
        monkeypatch.setattr(session_routing, "_routing_enabled", True)
        assert totals() == (1, 2, "空闲 3")
        monkeypatch.setattr(session_routing, "_routing_enabled", False)
        token = TurnToken(message_id="handling-1", client_message_id=None, channel="web", chat="console",
                          received_at=NOW - timedelta(minutes=3), ops_turn=Turn(channel="web", chat="console",
                                                                                origin=TurnOrigin.USER))
        main.running_token = token
        main_queued, other_queued, text = totals()
        assert (main_queued, other_queued, text) == (2, 2, "answering 4")
        answering = await status_card.snapshot(
            fleet=status_card.SoloFleet(manager), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "管家：在回答你（控制台，3 分钟）；排队 4 条" in answering["text"]
        assert "空闲" not in answering["text"]
        manager._bus = None
        manager._handling = None
        manager._router._queues[key.text] = asyncio.Queue()
        card = await status_card.snapshot(
            fleet=status_card.SoloFleet(manager), task_router=SimpleNamespace(running_planners=lambda: []),
            worker_pool=None, now=NOW)
        assert "在回答你（控制台，3 分钟）；排队 1 条" in card["text"]
        assert card["sections"][0]["managers"][0]["sessions"][0]["state"] == "answering"

        main.running_token = None
        await main.turn_lock.acquire()
        try:
            activity = status_card.session_activity(manager)
        finally:
            main.turn_lock.release()
        assert activity[0].state == "answering" and activity[0].queued == 1

    asyncio.run(check())


def _manager_line(activity: list[SessionActivity]) -> list[str]:
    """The queue total the card would say, plus each session's state, for the accounting test."""
    idle = all(item.state == "idle" for item in activity)
    return [f"{'空闲' if idle else activity[0].state} {sum(item.queued for item in activity)}"]
