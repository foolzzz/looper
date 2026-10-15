"""The scheduler's decisions on due fires, and the run context it appends to a run's requirement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from looper.agents.scheduler import Action, Firing, Trigger, decide, run_context

NOW = datetime(2026, 10, 15, 16, 0, 30, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)


def _due(*minutes_ago: int) -> list[datetime]:
    return [NOW.replace(second=0) - m * MINUTE for m in sorted(minutes_ago, reverse=True)]


@pytest.mark.parametrize(("due", "catch_up", "action", "trigger", "missed"), [
    ([], "once", Action.NONE, None, 0),
    # One fire, on time.
    (_due(0), "once", Action.FIRE, Trigger.SCHEDULED, 0),
    (_due(0), "skip", Action.FIRE, Trigger.SCHEDULED, 0),
    # One fire, missed by more than the grace period (the computer slept through it).
    (_due(30), "once", Action.FIRE, Trigger.CATCH_UP, 1),
    (_due(30), "skip", Action.SKIP, Trigger.SCHEDULED, 1),
    # Several missed fires: once runs one catch-up for all of them.
    (_due(3, 2, 1), "once", Action.FIRE, Trigger.CATCH_UP, 3),
    # skip runs the latest when it is within 2 minutes, and skips them all otherwise.
    (_due(3, 2, 1), "skip", Action.FIRE, Trigger.SCHEDULED, 2),
    (_due(9, 6, 3), "skip", Action.SKIP, Trigger.SCHEDULED, 3),
])
def test_missed_fire_decisions(due: list[datetime], catch_up: str, action: Action, trigger: Trigger | None,
                               missed: int) -> None:
    decision = decide(due, latest=due[-1] if due else None, now=NOW, catch_up=catch_up)
    assert decision.action is action
    if action is Action.NONE:
        assert decision.firing is None
        return
    assert decision.firing is not None
    assert (decision.firing.trigger, decision.firing.missed) == (trigger, missed)
    assert decision.firing.scheduled_for == due[-1]
    assert decision.firing.missed_since == (due[0] if missed else None)


def test_the_latest_fire_can_lie_past_a_cut_count() -> None:
    due = _due(50, 40)  # counted up to the limit; the latest fire is later
    decision = decide(due, latest=NOW.replace(second=0), now=NOW, catch_up="skip")
    assert (decision.action, decision.firing.scheduled_for) == (Action.FIRE, NOW.replace(second=0))


SCHEDULE = {"name": "Nightly triage", "cron": "*/30 * * * *", "timezone": "America/Los_Angeles"}


def test_the_run_context_names_the_firing_the_missed_fires_and_the_recent_runs() -> None:
    firing = Firing(Trigger.CATCH_UP, NOW.replace(second=0), missed=4, missed_since=NOW - 90 * MINUTE)
    recent = [
        {"status": "completed", "trigger": "manual", "fired_at": NOW - 3 * timedelta(hours=1), "scheduled_for": NOW,
         "summary": "Found 2 new alerts.\n" + "x" * 400},
        {"status": "skipped_overlap", "trigger": "scheduled", "fired_at": None,
         "scheduled_for": NOW - 4 * timedelta(hours=1), "summary": None,
         "error": "the previous run's task is still in progress"},
    ]
    text = run_context(SCHEDULE, firing, recent, description="Every 30 minutes")
    assert text.startswith("## 定时运行上下文\n")
    assert "「Nightly triage」" in text and "`*/30 * * * *`（Every 30 minutes）" in text
    assert "2026-10-15 09:00 PDT（America/Los_Angeles）" in text  # the fire time, in the schedule's zone
    assert "漏掉了 4 次触发" in text and "2026-10-15 07:30 PDT" in text
    assert "Found 2 new alerts. xxx" in text and "x" * 301 not in text  # one line, truncated
    assert "06:00 PDT（手动「立即运行」） · 完成" in text
    assert "跳过（上一次运行还没结束）" in text
    assert text.rstrip().endswith("并把假设写进结果。")


def test_a_first_manual_run_says_so() -> None:
    text = run_context(SCHEDULE, Firing(Trigger.MANUAL, NOW), [], description="Every 30 minutes")
    assert "手动「立即运行」" in text and "这是第一次" in text and "漏" not in text


def test_the_gate_output_and_the_previous_unfinished_input_share_the_room() -> None:
    from looper.agents.scheduler import Carried

    schedule = {**SCHEDULE, "gate_command": "python3 new_alerts.py"}
    carried = Carried("srun-1", "failed", NOW - 30 * MINUTE, "alert-7\nalert-8")
    text = run_context(schedule, Firing(Trigger.SCHEDULED, NOW), [], description="Every 30 minutes",
                       gate_output="alert-9 ```code```\n" + "y" * 2000, carried=carried, room=900)
    assert "- 前置检查（`python3 new_alerts.py`）的输出就是这一轮的输入：只处理这些，不要再自己运行这条命令或读它的来源去找别的条目：\n````\nalert-9 ```code```" in text
    assert "[截断：还有 1419 个字符，完整内容见这次运行的记录]" in text  # two thirds of 900 for its own output
    assert "- 上一轮未完成：2026-10-15 08:30 PDT 的运行失败" in text and "```\nalert-7\nalert-8\n```" in text
    assert text.index("前置检查（") < text.index("上一轮未完成") < text.index("这是第一次")
    # Without them the section is as before.
    assert "前置检查" not in run_context(schedule, Firing(Trigger.SCHEDULED, NOW), [], description="d")


@pytest.mark.parametrize(("notify", "status", "sent"), [
    ("failures", "failed", True), ("failures", "timed_out", True), ("failures", "gate_failed", True),
    ("failures", "completed", False), ("failures", "stopped", False), ("failures", "skipped_gate", False),
    ("always", "completed", True), ("always", "stopped", True), ("always", "gate_failed", True),
    ("always", "skipped_gate", False), ("always", "skipped_budget", False), ("always", "skipped_overlap", False),
    ("never", "failed", False), ("never", "completed", False),
])
def test_which_runs_get_a_message(notify: str, status: str, sent: bool) -> None:
    from looper.agents.schedule_notify import wants_run_message

    assert wants_run_message(notify, status) is sent


@pytest.mark.parametrize(("rule", "last_ok", "first_run", "deadline"), [
    # Every 30 minutes: 3 periods after the first fire after the last success.
    ("*/30 * * * *", NOW.replace(second=0), None, NOW.replace(second=0) + 120 * MINUTE),
    # Daily: 24 hours, shorter than 3 days.
    ("0 9 * * *", NOW.replace(second=0), None, NOW.replace(second=0) + timedelta(days=2)),
    # Never a success: counted from the first run.
    ("*/30 * * * *", None, NOW.replace(second=0), NOW.replace(second=0) + 90 * MINUTE),
    ("*/30 * * * *", None, None, None),
])
def test_when_the_no_success_alert_is_due(rule: str, last_ok: datetime | None, first_run: datetime | None,
                                          deadline: datetime | None) -> None:
    from looper.agents import cron
    from looper.agents.schedule_notify import stale_deadline

    tz = cron.zone("America/Los_Angeles")
    assert stale_deadline(cron.parse(rule), tz, last_ok=last_ok, first_run=first_run) == deadline
