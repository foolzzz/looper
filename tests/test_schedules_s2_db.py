"""Schedules S2 against a real PostgreSQL: the gate, the previous run's unfinished input, the daily budget, the
messages and the two alerts (sent through a stand-in, never a real channel), and the dashboard summary.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database; it uses the S1 tests' helpers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents import cron
from tests.test_schedules_db import DSN, NOON, _Env, _run

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

LA = cron.zone("America/Los_Angeles")
TELEGRAM_CHAT = "1000001"  # a stand-in id in the test database's allowlist; nothing is sent anywhere


class _Sender:
    """Stands in for the server's channels: records each message instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail_with: str | None = None

    async def __call__(self, channel: str, chat: str, text: str) -> tuple[bool, str | None]:
        if self.fail_with:
            return False, self.fail_with
        self.sent.append((channel, chat, text))
        return True, None


@pytest.fixture(autouse=True)
def _short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    from looper.agents import proc_tree

    monkeypatch.setattr(proc_tree, "TERM_GRACE_SEC", 0.2)  # a timed-out gate's tree is ended quickly


def _setup(env: _Env, *, day_zone: str = "America/Los_Angeles", dashboard_url: str = "") -> _Sender:
    from looper.agents.scheduler import Scheduler

    sender = _Sender()
    env.scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=sender, day_zone=day_zone,
                              dashboard_url=dashboard_url)
    return sender


async def _allow_telegram(env: _Env) -> None:
    from looper.agents.serve import seed_channel_configs

    await seed_channel_configs(env.pool)
    await env.pool.execute(
        "UPDATE channel_configs SET config = config || $1::jsonb WHERE channel_id = 'channel-telegram'",
        f'{{"allowed_user_ids": ["{TELEGRAM_CHAT}"]}}')


async def _fire(env: _Env, sid: str) -> dict[str, Any]:
    """Fire ``sid`` alone on the next fire time (the other schedules wait), and wait for its gate and messages."""
    await env.due(sid)
    await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE enabled AND id <> $1", sid,
                           env.clock.now + timedelta(days=30))
    await env.scheduler.tick()
    await env.scheduler.idle()
    return (await env.runs(sid))[0]


async def _requirement(env: _Env, task_id: str) -> str:
    return await env.pool.fetchval("SELECT requirement FROM tasks WHERE id = $1", task_id)


async def _outbox(env: _Env, sid: str) -> list[dict[str, Any]]:
    return list(reversed((await env.ok("GET", f"/api/schedules/{sid}/notifications"))["notifications"]))


def test_the_gate_decides_whether_a_run_gets_a_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        items = tmp_path / "items.txt"
        items.write_text("")
        sid = (await env.enabled(gate_command="cat items.txt"))["schedule_id"]

        nothing = await _fire(env, sid)
        assert (nothing["status"], nothing["task_id"], nothing["gate_output"]) == ("skipped_gate", None, None)
        assert env.bus.of("task_start") == []
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["consecutive_failures"] == 0

        items.write_text("alert-1\nalert-2\n")
        found = await _fire(env, sid)
        assert (found["status"], found["gate_output"]) == ("running", "alert-1\nalert-2")
        assert env.bus.of("task_start") == [found["task_id"]]
        assert "的输出就是这一轮的输入：只处理这些，不要再自己运行这条命令或读它的来源去找别的条目：\n```\nalert-1\nalert-2\n```" in await _requirement(env, found["task_id"])
        await env.end_task(found["task_id"], "completed")

        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "echo half; echo broken >&2; exit 4"})
        failed = await _fire(env, sid)
        assert (failed["status"], failed["task_id"], failed["gate_output"]) == ("gate_failed", None, "half")
        assert failed["error"] == "the gate exited with status 4; stderr: broken"
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["consecutive_failures"] == 1

        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "trap '' TERM; sleep 30",
                                                      "gate_timeout_seconds": 1})
        timed_out = await _fire(env, sid)
        assert timed_out["status"] == "gate_failed" and "did not finish within 1 s" in timed_out["error"]

    _run(test, monkeypatch, tmp_path)


def test_the_longest_schedule_requirement_and_a_large_input_still_fit_a_tasks_requirement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The schedule's own requirement leaves room under the task limit for the run context, which the gate's output
    is fitted into: the run's task takes its requirement whole, never clipped."""
    from looper.agents.schedules import REQUIREMENT_MAX_CHARS
    from looper.agents.system_prompt import TASK_REQUIREMENT_LIMIT

    async def test(env: _Env) -> None:
        _setup(env)
        (tmp_path / "items.txt").write_text("alert-123456789\n" * 10_000)
        sid = (await env.enabled(requirement="x" * REQUIREMENT_MAX_CHARS, gate_command="cat items.txt"))["schedule_id"]
        run = await _fire(env, sid)
        requirement = await _requirement(env, run["task_id"])
        assert requirement.startswith("x" * REQUIREMENT_MAX_CHARS + "\n\n## 定时运行上下文")
        assert "alert-123456789" in requirement and len(requirement) <= TASK_REQUIREMENT_LIMIT

    _run(test, monkeypatch, tmp_path)


def test_run_now_goes_through_the_gate_and_a_firing_meanwhile_is_an_overlap(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.schedule(gate_command="sleep 1; echo new-item"))["schedule_id"]
        first = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        assert (first["trigger"], first["status"]) == ("manual", "gating")
        second = (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]
        assert second["status"] == "skipped_overlap" and "still checking its gate" in second["error"]
        await env.scheduler.idle()
        run = next(r for r in await env.runs(sid) if r["run_id"] == first["run_id"])
        assert (run["status"], run["gate_output"]) == ("running", "new-item")

    _run(test, monkeypatch, tmp_path)


def test_a_gate_the_server_stopped_during_is_a_failed_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.schedule(gate_command="true"))["schedule_id"]
        await env.pool.execute(
            "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status) "
            "VALUES ('srun-000000000001', $1, now(), now(), 'scheduled', 'gating')", sid)
        await env.scheduler.start()
        await env.scheduler.stop()
        [run] = await env.runs(sid)
        assert (run["status"], run["error"]) == ("gate_failed", "the server stopped while the gate ran")

    _run(test, monkeypatch, tmp_path)


def test_the_previous_runs_unfinished_input_is_handed_on_once(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        items = tmp_path / "items.txt"
        items.write_text("alert-7\n")
        sid = (await env.enabled(gate_command="cat items.txt"))["schedule_id"]
        first = await _fire(env, sid)
        await env.end_task(first["task_id"], "failed", summary="the API was down")

        # The gate finds nothing new, but the failed run's input was not done: the run goes ahead with it.
        items.write_text("")
        second = await _fire(env, sid)
        assert (second["status"], second["carried_from"], second["gate_output"]) == ("running", first["run_id"], None)
        requirement = await _requirement(env, second["task_id"])
        assert "- 上一轮未完成：" in requirement and "的运行失败" in requirement and "```\nalert-7\n```" in requirement
        await env.end_task(second["task_id"], "completed")

        # That run completed: nothing is handed on, and nothing new is a skip.
        third = await _fire(env, sid)
        assert (third["status"], third["carried_from"]) == ("skipped_gate", None)

    _run(test, monkeypatch, tmp_path)


def test_the_daily_budget_skips_runs_until_the_next_day(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled(daily_budget_usd=0.5))["schedule_id"]
        env.clock.now = datetime(2026, 10, 20, 23, 40, tzinfo=LA)
        first = await _fire(env, sid)  # 23:45
        await env.end_task(first["task_id"], "completed", cost=0.6)

        skipped = await _fire(env, sid)  # 23:50, the same day
        assert (skipped["status"], skipped["task_id"]) == ("skipped_budget", None)
        assert "daily budget is $0.5" in skipped["error"]
        assert env.bus.of("task_start") == [first["task_id"]]

        env.clock.now = datetime(2026, 10, 21, 0, 0, 5, tzinfo=LA)
        next_day = await _fire(env, sid)  # 00:05, a new day
        assert next_day["status"] == "running"

    _run(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("notify", ["failures", "always", "never"])
def test_a_runs_message_follows_notify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, notify: str) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env, dashboard_url="https://looper.example.com")
        await _allow_telegram(env)
        sid = (await env.enabled(name="Alert triage", notify=notify,
                                 notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT}))["schedule_id"]
        done = await _fire(env, sid)
        await env.end_task(done["task_id"], "completed", summary="2 new alerts, both known", cost=0.05)
        failed = await _fire(env, sid)
        await env.end_task(failed["task_id"], "failed", summary="the API was down")
        await env.scheduler.idle()

        expected = {"failures": ["失败"], "always": ["完成", "失败"], "never": []}[notify]
        assert [text.splitlines()[0].rsplit("：", 1)[1] for _, _, text in sender.sent] == expected
        if notify == "always":
            channel, chat, text = sender.sent[0]
            assert (channel, chat) == ("telegram", TELEGRAM_CHAT)
            assert text.startswith("定时任务「Alert triage」（项目 alerts）") and "2 new alerts, both known" in text
            assert text.endswith(f"花费 $0.05 · https://looper.example.com/#tasks/{done['task_id']}")
        assert [n["status"] for n in await _outbox(env, sid)] == ["sent"] * len(expected)

    _run(test, monkeypatch, tmp_path)


def test_the_failures_alert_goes_out_once_per_streak_with_one_recovery(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env)
        await _allow_telegram(env)
        sid = (await env.enabled(gate_command="exit 1",
                                 notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT}))["schedule_id"]

        async def kinds() -> list[str]:  # the no-success alert has its own test
            return [n["kind"] for n in await _outbox(env, sid) if not n["kind"].startswith("stale")]

        for _ in range(4):
            await _fire(env, sid)
        await env.scheduler.idle()
        assert await kinds() == ["run", "run", "run", "failures_alert", "run"]
        alert = [n for n in await _outbox(env, sid) if n["kind"] == "failures_alert"][0]
        assert "已连续失败 3 次" in alert["text"] and alert["status"] == "sent"
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["alerts"]["failures_since"]

        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "echo item"})
        recovered = await _fire(env, sid)
        await env.end_task(recovered["task_id"], "completed")
        await env.scheduler.idle()
        again = await _fire(env, sid)
        await env.end_task(again["task_id"], "completed")
        await env.scheduler.idle()
        assert (await kinds())[5:] == ["failures_recovered"]  # once; completed runs send nothing under "failures"
        schedule = (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]
        assert (schedule["alerts"]["failures_since"], schedule["consecutive_failures"]) == (None, 0)
        assert len([text for _, _, text in sender.sent if "仍没有成功运行" not in text and "又有成功" not in text]) == 6

    _run(test, monkeypatch, tmp_path)


def test_a_gate_that_finds_nothing_ends_the_failure_streak_and_a_skip_that_ran_nothing_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A clean gate with nothing new is an ok run, as for the no-success alert (``last_ok_at``): the streak ends
    there with its recovery message, not at the next completed task, which for a gated schedule can be hours
    away. A firing that ran nothing (busy, over budget, missed) says nothing about the schedule's health."""
    from looper.db import schedules as db_schedules

    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        await _allow_telegram(env)
        sid = (await env.enabled(gate_command="exit 3",
                                 notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT}))["schedule_id"]

        async def state() -> tuple[int, bool, list[str]]:
            schedule = (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]
            kinds = [n["kind"] for n in await _outbox(env, sid) if n["kind"].startswith("failures")]
            return schedule["consecutive_failures"], bool(schedule["alerts"]["failures_since"]), kinds

        for _ in range(3):
            await _fire(env, sid)
        assert await state() == (3, True, ["failures_alert"])

        # A firing while the gate runs is skipped as busy: the streak goes on.
        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "sleep 1; exit 3"})
        assert (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]["status"] == "gating"
        assert (await env.ok("POST", f"/api/schedules/{sid}/run"))["run"]["status"] == "skipped_overlap"
        assert await state() == (3, True, ["failures_alert"])
        await env.scheduler.idle()
        assert await state() == (4, True, ["failures_alert"])
        for status in db_schedules.NOTHING_RAN:  # nor does a skip for the budget or a missed firing
            await db_schedules.count_outcome(env.pool, sid, status, env.clock.now)
        assert await state() == (4, True, ["failures_alert"])

        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "true"})
        assert (await _fire(env, sid))["status"] == "skipped_gate"
        assert await state() == (0, False, ["failures_alert", "failures_recovered"])
        # An ok run, not a success: no task completed.
        success, ok = await env.pool.fetchrow("SELECT last_success_at, last_ok_at FROM schedules WHERE id = $1", sid)
        assert success is None and ok is not None
        assert (await _fire(env, sid))["firings"] == 2  # the next one is counted on it, and recovers nothing more
        assert await state() == (0, False, ["failures_alert", "failures_recovered"])

    _run(test, monkeypatch, tmp_path)


def test_without_a_target_messages_stay_on_the_dashboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env)
        sid = (await env.enabled(gate_command="exit 1"))["schedule_id"]
        for _ in range(3):
            await _fire(env, sid)
        await env.scheduler.idle()
        outbox = await _outbox(env, sid)
        assert [(n["kind"], n["status"], n["channel"]) for n in outbox] == [
            ("run", "dashboard", None)] * 3 + [("failures_alert", "dashboard", None)]
        assert sender.sent == []

    _run(test, monkeypatch, tmp_path)


def test_a_message_the_channel_could_not_send_is_recorded_failed(monkeypatch: pytest.MonkeyPatch,
                                                                 tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env)
        sender.fail_with = "the telegram channel is not running: enable it in Settings → Channels"
        await _allow_telegram(env)
        sid = (await env.enabled(gate_command="exit 1",
                                 notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT}))["schedule_id"]
        await _fire(env, sid)
        await env.scheduler.idle()
        [note] = await _outbox(env, sid)
        assert (note["status"], note["error"]) == ("failed", sender.fail_with)

    _run(test, monkeypatch, tmp_path)


def test_the_no_success_alert_goes_out_once_with_one_recovery(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        ok = await _fire(env, sid)
        await env.end_task(ok["task_id"], "completed")
        # That run's end is reported (its recoveries, none here) before the clock moves on: otherwise its recovery
        # check could find the stale alert raised below and end it.
        await env.scheduler.idle()
        last_ok = await env.pool.fetchval("SELECT last_ok_at FROM schedules WHERE id = $1", sid)
        # Every 5 minutes: due 3 periods after the first fire after the last success.
        first_chance = cron.next_fire(cron.parse("*/5 * * * *"), LA, last_ok)
        later = first_chance + timedelta(minutes=15)
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, later + timedelta(days=1))

        env.clock.now = later - timedelta(seconds=1)
        await env.scheduler.tick()
        env.clock.now = later
        await env.scheduler.tick()
        await env.scheduler.tick()
        await env.scheduler.idle()
        assert [n["kind"] for n in await _outbox(env, sid)] == ["stale_alert"]
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["alerts"]["stale_since"]

        recovered = await _fire(env, sid)
        await env.end_task(recovered["task_id"], "completed")
        await env.scheduler.idle()
        assert [n["kind"] for n in await _outbox(env, sid)] == ["stale_alert", "stale_recovered"]

    _run(test, monkeypatch, tmp_path)


def test_notify_alerts_sends_the_alerts_and_their_recoveries_and_no_run_message(monkeypatch: pytest.MonkeyPatch,
                                                                                tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        sender = _setup(env)
        await _allow_telegram(env)
        sid = (await env.enabled(gate_command="exit 3", notify="alerts",
                                 notify_target={"channel": "telegram", "chat": TELEGRAM_CHAT}))["schedule_id"]
        assert (await env.ok("GET", f"/api/schedules/{sid}"))["schedule"]["notify"] == "alerts"

        async def sent() -> list[tuple[str, str]]:
            return [(n["kind"], n["status"]) for n in await _outbox(env, sid)]

        for _ in range(3):
            await _fire(env, sid)
        assert await sent() == [("failures_alert", "sent")]  # no 「前置检查失败」 message per run
        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "true"})
        assert (await _fire(env, sid))["status"] == "skipped_gate"
        assert await sent() == [("failures_alert", "sent"), ("failures_recovered", "sent")]

        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "echo item"})
        done = await _fire(env, sid)
        await env.end_task(done["task_id"], "completed")
        await env.scheduler.idle()
        assert len(await sent()) == 2  # a completed run says nothing either

        # No success for 3 periods: the stale alert, then its recovery, still go out.
        last_ok = await env.pool.fetchval("SELECT last_ok_at FROM schedules WHERE id = $1", sid)
        later = cron.next_fire(cron.parse("*/5 * * * *"), LA, last_ok) + timedelta(minutes=15)
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, later + timedelta(days=1))
        env.clock.now = later
        await env.scheduler.tick()
        await env.scheduler.idle()
        await env.ok("PUT", f"/api/schedules/{sid}", {"gate_command": "true"})
        await _fire(env, sid)
        assert [kind for kind, _ in await sent()][2:] == ["stale_alert", "stale_recovered"]
        assert len(sender.sent) == 4 and all(status == "sent" for _, status in await sent())

    _run(test, monkeypatch, tmp_path)


def test_a_database_from_before_takes_notify_alerts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import connection

    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        await env.pool.execute("ALTER TABLE schedules DROP CONSTRAINT schedules_notify_check")
        await env.pool.execute("ALTER TABLE schedules ADD CONSTRAINT schedules_notify_check "
                               "CHECK (notify IN ('failures', 'always', 'never'))")
        await env.pool.execute("DELETE FROM schema_migrations WHERE name = 'schedules_notify_alerts'")
        await connection.ensure_schema(env.pool)
        assert (await env.ok("PUT", f"/api/schedules/{sid}", {"notify": "alerts"}))["schedule"]["notify"] == "alerts"
        status, answer = await env.call("PUT", f"/api/schedules/{sid}", {"notify": "quiet"})
        assert status == 400 and "notify" in answer["error"]

    _run(test, monkeypatch, tmp_path)


MINUTE = timedelta(minutes=1)


async def _next_minute(env: _Env, sid: str) -> list[dict[str, Any]]:
    """Fire ``sid`` at the next minute (the clock 10 s after it), wait for its gate; its runs, newest first."""
    at = (env.clock.now + MINUTE).replace(second=0, microsecond=0)
    await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, at)
    env.clock.now = at + timedelta(seconds=10)
    await env.scheduler.tick()
    await env.scheduler.idle()
    return await env.runs(sid)


def _shape(runs: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(run["status"], run["firings"]) for run in runs]


def test_a_gated_schedule_every_minute_counts_its_repeated_skips_on_one_run(monkeypatch: pytest.MonkeyPatch,
                                                                           tmp_path: Path) -> None:
    from looper.agents.schedules import schedule_overview

    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        items = tmp_path / "items.txt"
        items.write_text("")
        sid = (await env.enabled(cron="* * * * *", gate_command="cat items.txt"))["schedule_id"]

        for n in (1, 2, 3):  # nothing new: one run counts the three firings, each keeping the schedule healthy
            # As if no success had come for too long: each of these firings, counted or not, ends that alert.
            await env.pool.execute("UPDATE schedules SET stale_alert_at = $2 WHERE id = $1", sid, NOON)
            runs = await _next_minute(env, sid)
            assert _shape(runs) == [("skipped_gate", n)]
            ok_at = await env.pool.fetchval("SELECT last_ok_at FROM schedules WHERE id = $1", sid)
            assert ok_at == datetime.fromisoformat(runs[0]["finished_at"]) == env.clock.now
        assert datetime.fromisoformat(runs[0]["scheduled_for"]) == NOON + MINUTE
        assert [n["kind"] for n in await _outbox(env, sid)] == ["stale_recovered"] * 3

        items.write_text("alert-1\n")
        runs = await _next_minute(env, sid)
        assert _shape(runs) == [("running", 1), ("skipped_gate", 3)]
        task_id = runs[0]["task_id"]
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", task_id)
        for n in (1, 2):  # its task still runs: every minute after is one repeated overlap skip
            assert _shape(await _next_minute(env, sid))[0] == ("skipped_overlap", n)
        await env.end_task(task_id, "completed", summary="triaged alert-1")
        await env.scheduler.idle()
        items.write_text("")
        assert _shape(await _next_minute(env, sid)) == [
            ("skipped_gate", 1), ("skipped_overlap", 2), ("completed", 1), ("skipped_gate", 3)]

        # The dashboard counts every skipped firing of the day, not the runs.
        skipped = (await schedule_overview(NOON - timedelta(hours=12)))["skipped_today"]
        assert (skipped["skipped_gate"], skipped["skipped_overlap"]) == (4, 2)
        # So does a schedule's run history, over its whole history, shown or left out.
        status, listed = await env.call("GET", f"/api/schedules/{sid}/runs", query={"skips": "hide"})
        assert (status, listed["routine_skips"]) == (200, {"skipped_gate": 4, "skipped_overlap": 2})
        assert [run["status"] for run in listed["runs"]] == ["completed"]
        assert env.bus.of("task_start") == [task_id]

    _run(test, monkeypatch, tmp_path)


def test_a_skip_on_a_new_day_is_a_run_of_its_own(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        (tmp_path / "items.txt").write_text("")
        sid = (await env.enabled(cron="* * * * *", gate_command="cat items.txt"))["schedule_id"]
        env.clock.now = datetime(2030, 1, 16, 7, 58, 30, tzinfo=timezone.utc)  # 23:58:30 in Los Angeles
        await _next_minute(env, sid)
        assert _shape(await _next_minute(env, sid)) == [("skipped_gate", 1), ("skipped_gate", 1)]

    _run(test, monkeypatch, tmp_path)


def test_a_busy_streak_and_the_daily_budget_start_again_at_midnight(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        (tmp_path / "items.txt").write_text("alert-1\n")
        sid = (await env.enabled(cron="* * * * *", gate_command="cat items.txt",
                                 daily_budget_usd=0.5))["schedule_id"]
        env.clock.now = datetime(2030, 1, 16, 7, 56, 30, tzinfo=timezone.utc)  # 23:56:30 in Los Angeles
        [run] = await _next_minute(env, sid)  # 23:57
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])
        for _ in range(3):  # its task still runs: 23:58 and 23:59 are one busy skip, 00:00 a new day's
            runs = await _next_minute(env, sid)
        assert _shape(runs) == [("skipped_overlap", 1), ("skipped_overlap", 2), ("running", 1)]

        # The run fired yesterday: its cost is yesterday's, so 00:01 runs; that run's own cost counts today.
        await env.end_task(run["task_id"], "completed", cost=0.6)
        today = (await _next_minute(env, sid))[0]
        assert today["status"] == "running"
        await env.end_task(today["task_id"], "completed", cost=0.6)
        over = (await _next_minute(env, sid))[0]  # 00:02
        assert over["status"] == "skipped_budget" and "today's runs cost $0.6000" in over["error"]

    _run(test, monkeypatch, tmp_path)


def test_a_slow_gate_never_runs_twice_at_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        sid = (await env.enabled(cron="* * * * *",
                                 gate_command="echo start >> gate.log; sleep 1; echo end >> gate.log"))["schedule_id"]
        for _ in range(3):  # three minutes come while the first gate runs
            at = (env.clock.now + MINUTE).replace(second=0, microsecond=0)
            await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, at)
            env.clock.now = at + timedelta(seconds=10)
            await env.scheduler.tick()
        runs = await env.runs(sid)
        assert _shape(runs) == [("skipped_overlap", 2), ("gating", 1)]
        assert runs[0]["error"] == "the previous run is still checking its gate"
        await env.scheduler.idle()
        assert (tmp_path / "gate.log").read_text() == "start\nend\n"
        assert _shape(await env.runs(sid)) == [("skipped_overlap", 2), ("skipped_gate", 1)]

    _run(test, monkeypatch, tmp_path)


EVERY_5 = cron.parse("*/5 * * * *")


async def _stale(env: _Env, sid: str) -> list[str]:
    """The schedule's no-success messages so far, after a scheduler pass at the clock's time."""
    await env.scheduler.tick()
    await env.scheduler.idle()
    return [n["kind"] for n in await _outbox(env, sid) if n["kind"].startswith("stale")]


async def _ran_without_success(env: _Env, sid: str, *, days_ago: int) -> None:
    """One run that did not succeed (stopped: no message under notify failures), its fire and the schedule's
    enabling moved ``days_ago`` days back; the schedule does not fire again for a day."""
    run = await _fire(env, sid)
    await env.end_task(run["task_id"], "stopped")
    await env.scheduler.idle()
    back = timedelta(days=days_ago)
    await env.pool.execute(
        "UPDATE schedule_runs SET scheduled_for = scheduled_for - $2::interval WHERE schedule_id = $1", sid, back)
    await env.pool.execute(
        "UPDATE schedules SET enabled_at = enabled_at - $2::interval, next_fire_at = $3 WHERE id = $1",
        sid, back, env.clock.now + timedelta(days=1))


async def _enable_again(env: _Env, sid: str) -> datetime:
    """Disable the schedule and enable it again through the API; when it was enabled. It does not fire for a day."""
    await env.ok("PUT", f"/api/schedules/{sid}", {"enabled": False})
    await env.ok("PUT", f"/api/schedules/{sid}", {"enabled": True})
    enabled_at = await env.pool.fetchval("SELECT enabled_at FROM schedules WHERE id = $1", sid)
    await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid,
                           enabled_at + timedelta(days=1))
    return enabled_at


def test_enabling_a_schedule_whose_last_success_is_old_raises_no_alert_at_once(monkeypatch: pytest.MonkeyPatch,
                                                                               tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        ok = await _fire(env, sid)
        await env.end_task(ok["task_id"], "completed")
        await env.scheduler.idle()
        # Paused two days after that success, then enabled again.
        await env.pool.execute("UPDATE schedules SET last_ok_at = last_ok_at - interval '2 days' WHERE id = $1", sid)
        enabled_at = await _enable_again(env, sid)
        # Due 3 periods after the first fire after the enabling, not after the old success.
        due = cron.next_fire(EVERY_5, LA, enabled_at) + timedelta(minutes=15)
        env.clock.now = enabled_at + timedelta(seconds=1)
        assert await _stale(env, sid) == []
        env.clock.now = due - timedelta(seconds=1)
        assert await _stale(env, sid) == []
        env.clock.now = due
        assert await _stale(env, sid) == ["stale_alert"]

    _run(test, monkeypatch, tmp_path)


def test_a_schedule_enabled_long_ago_without_a_success_still_alerts(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        await _ran_without_success(env, sid, days_ago=7)
        assert await _stale(env, sid) == ["stale_alert"]

    _run(test, monkeypatch, tmp_path)


def test_disabling_and_enabling_again_starts_the_clock_again(monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        await _ran_without_success(env, sid, days_ago=7)  # it would be stale now
        await env.ok("PUT", f"/api/schedules/{sid}", {"enabled": False})
        assert await _stale(env, sid) == []  # a disabled schedule is never stale
        enabled_at = await _enable_again(env, sid)
        due = cron.next_fire(EVERY_5, LA, enabled_at) + timedelta(minutes=15)
        env.clock.now = enabled_at + timedelta(seconds=1)
        assert await _stale(env, sid) == []
        env.clock.now = due
        assert await _stale(env, sid) == ["stale_alert"]
        # Enabling an enabled schedule changes nothing.
        await env.ok("PUT", f"/api/schedules/{sid}", {"enabled": True})
        assert await env.pool.fetchval("SELECT enabled_at FROM schedules WHERE id = $1", sid) == enabled_at

    _run(test, monkeypatch, tmp_path)


def test_a_schedule_from_before_counts_from_its_last_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        from looper.db import connection

        sid = (await env.enabled())["schedule_id"]
        # A database from before enabled_at, whose schedule was last changed three days ago.
        await env.pool.execute("ALTER TABLE schedules DROP COLUMN enabled_at")
        await env.pool.execute("UPDATE schedules SET updated_at = updated_at - interval '3 days' WHERE id = $1", sid)
        for _ in range(2):  # the migration runs once
            await connection.ensure_schema(env.pool)
            row = await env.pool.fetchrow("SELECT enabled_at, updated_at FROM schedules WHERE id = $1", sid)
            assert row["enabled_at"] == row["updated_at"]
        created = (await env.schedule())["schedule_id"]
        assert await env.pool.fetchval("SELECT enabled_at IS NOT NULL FROM schedules WHERE id = $1", created)

    _run(test, monkeypatch, tmp_path)


def test_changing_the_rule_to_every_minute_raises_no_alert_at_once(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        (tmp_path / "items.txt").write_text("")
        sid = (await env.enabled(gate_command="cat items.txt"))["schedule_id"]  # every 5 minutes
        assert (await _fire(env, sid))["status"] == "skipped_gate"
        # The last ok firing was hours ago, as for a schedule whose gate failed for a while, enabled long ago.
        await env.pool.execute("UPDATE schedules SET last_ok_at = last_ok_at - interval '7 hours', "
                               "enabled_at = enabled_at - interval '2 days' WHERE id = $1", sid)
        changed = (await env.ok("PUT", f"/api/schedules/{sid}", {"cron": "* * * * *"}))["schedule"]
        assert changed["cron"] == "* * * * *"
        at = datetime.fromisoformat(changed["updated_at"])  # when the rule changed
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, at + timedelta(days=1))
        # The new rule's window counts from the change: nothing at once, the alert only once it has passed.
        due = cron.next_fire(cron.parse("* * * * *"), LA, at) + timedelta(minutes=15)
        env.clock.now = at + timedelta(seconds=1)
        assert await _stale(env, sid) == []
        env.clock.now = due - timedelta(seconds=1)
        assert await _stale(env, sid) == []
        env.clock.now = due
        assert await _stale(env, sid) == ["stale_alert"]

    _run(test, monkeypatch, tmp_path)


def test_a_run_that_succeeds_while_the_pass_looks_raises_no_stale_alert(monkeypatch: pytest.MonkeyPatch,
                                                                        tmp_path: Path) -> None:
    from looper.db import schedules as db_schedules

    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled())["schedule_id"]
        ok = await _fire(env, sid)
        await env.end_task(ok["task_id"], "completed")
        await env.scheduler.idle()
        long_run = await _fire(env, sid)
        await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid,
                               env.clock.now + timedelta(days=1))
        last_ok = await env.pool.fetchval("SELECT last_ok_at FROM schedules WHERE id = $1", sid)
        env.clock.now = cron.next_fire(EVERY_5, LA, last_ok) + timedelta(minutes=15)
        assert await _stale(env, sid) == []  # past its deadline, but its run is in progress

        # The pass has read the schedule when the long run succeeds, before it looks for the runs in progress.
        real_open_runs = db_schedules.open_runs

        async def succeeding_first(conn: Any, schedule_id: str) -> list[dict[str, Any]]:
            if schedule_id == sid and not ended:
                ended.append(long_run["task_id"])
                await env.end_task(long_run["task_id"], "completed")
                await env.scheduler.idle()  # its end is reported: no alert to recover from yet
            return await real_open_runs(conn, schedule_id)

        ended: list[str] = []
        monkeypatch.setattr(db_schedules, "open_runs", succeeding_first)
        assert await _stale(env, sid) == []
        assert ended == [long_run["task_id"]]
        assert await env.pool.fetchval("SELECT last_ok_at FROM schedules WHERE id = $1", sid) == env.clock.now
        # The deadline counts from that success now.
        env.clock.now += timedelta(minutes=20)
        assert await _stale(env, sid) == ["stale_alert"]

    _run(test, monkeypatch, tmp_path)


def test_a_minute_schedule_is_not_stale_while_its_task_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        items = tmp_path / "items.txt"
        items.write_text("")
        sid = (await env.enabled(cron="* * * * *", gate_command="cat items.txt",
                                 run_timeout_seconds=3600))["schedule_id"]
        assert _shape(await _next_minute(env, sid)) == [("skipped_gate", 1)]
        items.write_text("alert-1\n")
        runs = await _next_minute(env, sid)
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", runs[0]["task_id"])
        # Its task runs for 25 minutes (within its timeout): every minute is a busy skip, never a stale alert.
        for _ in range(25):
            runs = await _next_minute(env, sid)
        assert _shape(runs)[:2] == [("skipped_overlap", 25), ("running", 1)]
        assert [n["kind"] for n in await _outbox(env, sid) if n["kind"].startswith("stale")] == []
        await env.end_task(runs[1]["task_id"], "completed", summary="triaged")
        items.write_text("")
        assert _shape(await _next_minute(env, sid))[:2] == [("skipped_gate", 1), ("skipped_overlap", 25)]
        assert [n["kind"] for n in await _outbox(env, sid) if n["kind"].startswith("stale")] == []

    _run(test, monkeypatch, tmp_path)


# --- Reentrant schedules: overlap parallel ---


def test_a_parallel_schedule_starts_runs_beside_each_other_up_to_its_cap(monkeypatch: pytest.MonkeyPatch,
                                                                          tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        sid = (await env.enabled(overlap="parallel", max_parallel_runs=2))["schedule_id"]
        started = []
        for _ in range(2):
            run = await _fire(env, sid)
            assert run["status"] == "running"
            # As the TaskRouter would start it.
            await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])
            started.append(run)
        first, second = started
        over = await _fire(env, sid)
        assert over["status"] == "skipped_overlap" and over["task_id"] is None
        assert over["error"] == "2 runs are in progress, the most this schedule runs at once (max_parallel_runs)"
        assert env.bus.of("task_start") == [first["task_id"], second["task_id"]]
        await env.end_task(first["task_id"], "completed", summary="done")
        third = await _fire(env, sid)
        assert third["status"] == "running" and third["task_id"] not in (first["task_id"], second["task_id"])

    _run(test, monkeypatch, tmp_path)


def test_a_parallel_schedules_gates_never_run_beside_each_other_and_know_their_run(monkeypatch: pytest.MonkeyPatch,
                                                                                    tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        gate = ('echo start >> gate.log; sleep 1; echo end >> gate.log; '
                'echo "$LOOPER_SCHEDULE_ID $LOOPER_SCHEDULE_RUN_ID $LOOPER_SCHEDULE_SCHEDULED_FOR '
                '[$LOOPER_SCHEDULE_RUN_TIMEOUT_SECONDS]"')
        sid = (await env.enabled(cron="* * * * *", gate_command=gate, overlap="parallel",
                                 run_timeout_seconds=1800))["schedule_id"]
        for _ in range(2):  # a second minute comes while the first gate runs
            at = (env.clock.now + MINUTE).replace(second=0, microsecond=0)
            await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid, at)
            env.clock.now = at + timedelta(seconds=10)
            await env.scheduler.tick()
        skipped, gating = await env.runs(sid)
        assert (skipped["status"], skipped["error"]) == ("skipped_overlap", "the previous run is still checking its gate")
        await env.scheduler.idle()
        [run] = [r for r in await env.runs(sid) if r["run_id"] == gating["run_id"]]
        assert run["status"] == "running"
        assert run["gate_output"] == f"{sid} {run['run_id']} {(NOON + MINUTE).isoformat()} [1800]"
        assert (tmp_path / "gate.log").read_text() == "start\nend\n"
        # Its task runs; the next minute's gate runs (no other gate is running) and starts a run beside it.
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])
        assert _shape(await _next_minute(env, sid))[:2] == [("running", 1), ("skipped_overlap", 1)]

    _run(test, monkeypatch, tmp_path)


def test_a_parallel_schedules_daily_budget_counts_every_run_in_progress(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        sid = (await env.enabled(overlap="parallel", daily_budget_usd=0.5))["schedule_id"]
        for cost in (0.3, 0.25):  # two runs still in progress, each having spent this much so far
            run = await _fire(env, sid)
            await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])
            await env.pool.execute(
                "INSERT INTO llm_records (agent_id, task_id, input_text, output_text, status, cost_usd) "
                "VALUES ($1, $2, '', '', 'completed', $3)", f"planner-{run['task_id']}", run["task_id"], cost)
        third = await _fire(env, sid)
        assert third["status"] == "skipped_budget" and "today's runs cost $0.5500" in third["error"]

    _run(test, monkeypatch, tmp_path)


def test_a_failed_runs_input_goes_to_the_next_run_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        items = tmp_path / "items.txt"
        items.write_text("alert-1\n")
        sid = (await env.enabled(cron="* * * * *", gate_command="cat items.txt; : > items.txt",
                                 overlap="parallel"))["schedule_id"]
        [failing] = await _next_minute(env, sid)
        await env.end_task(failing["task_id"], "failed", summary="crashed")
        # The next run is given its input; while that run is in progress, the one after is not given it again.
        carrying = (await _next_minute(env, sid))[0]
        assert (carrying["status"], carrying["carried_from"]) == ("running", failing["run_id"])
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", carrying["task_id"])
        assert _shape(await _next_minute(env, sid))[0] == ("skipped_gate", 1)

    _run(test, monkeypatch, tmp_path)


def test_two_parallel_runs_that_fail_before_the_next_gate_both_hand_their_input_on_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: _Env) -> None:
        _setup(env)
        env.clock.now = NOON
        items = tmp_path / "items.txt"
        sid = (await env.enabled(cron="* * * * *", gate_command="cat items.txt; : > items.txt",
                                 overlap="parallel"))["schedule_id"]
        failing = []
        for n in (1, 2):  # two runs beside each other, each with its own alert
            items.write_text(f"alert-{n}\n")
            run = (await _next_minute(env, sid))[0]
            await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", run["task_id"])
            failing.append(run)
        for run in failing:  # both fail before the next gate runs, the first one first
            env.clock.now += timedelta(seconds=1)
            await env.end_task(run["task_id"], "failed", summary="crashed")
        carrying = (await _next_minute(env, sid))[0]
        assert (carrying["status"], carrying["gate_output"]) == ("running", None)
        assert carrying["carried_from"] == failing[1]["run_id"]  # the latest of them
        requirement = await _requirement(env, carrying["task_id"])
        assert requirement.count("- 上一轮未完成：") == 2
        assert requirement.index("```\nalert-1\n```") < requirement.index("```\nalert-2\n```")
        assert await env.pool.fetchval("SELECT bool_and(carried_on) FROM schedule_runs WHERE id = ANY($1::text[])",
                                       [run["run_id"] for run in failing])
        # Handed on once: while that run is in progress, the next gate finds nothing new and nothing is carried.
        await env.pool.execute("UPDATE tasks SET status = 'running' WHERE id = $1", carrying["task_id"])
        assert _shape(await _next_minute(env, sid))[0] == ("skipped_gate", 1)

    _run(test, monkeypatch, tmp_path)


def test_runs_from_before_hand_on_only_what_the_old_rule_would_have(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    from looper.db import connection

    async def test(env: _Env) -> None:
        sid = (await env.enabled(gate_command="cat items.txt"))["schedule_id"]
        rows = [("srun-old0000001", "failed", 3, None), ("srun-old0000002", "running", 2, "srun-old0000001"),
                ("srun-old0000003", "failed", 1, None), ("srun-old0000004", "skipped_overlap", 0, None)]
        for run_id, status, minutes_ago, carried_from in rows:
            ended = status not in ("running",)
            await env.pool.execute(
                "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, gate_output, "
                "finished_at, carried_from, created_at) VALUES ($1, $2, now() - $3::interval, now() - $3::interval, "
                "'scheduled', $4, 'alert', CASE WHEN $5 THEN now() - $3::interval END, $6, now() - $3::interval)",
                run_id, sid, timedelta(minutes=minutes_ago), status, ended, carried_from)
        await env.pool.execute("ALTER TABLE schedule_runs DROP COLUMN carried_on")
        await connection.ensure_schema(env.pool)
        handed = dict(await env.pool.fetch("SELECT id, carried_on FROM schedule_runs WHERE schedule_id = $1", sid))
        # The one carried on already is; the latest to end that ran something may still hand its input on.
        assert (handed["srun-old0000001"], handed["srun-old0000003"]) == (True, False)

    _run(test, monkeypatch, tmp_path)


def test_a_parallel_schedule_is_validated_and_answered_without_warnings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: _Env) -> None:
        for value in (0, 11, "3", True, 2.5):
            status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", {
                "name": f"p-{value}", "cron": "*/5 * * * *", "requirement": "r", "overlap": "parallel",
                "max_parallel_runs": value})
            assert status == 400 and "max_parallel_runs must be a whole number from 1 to 10" in payload["error"]
        plain = await env.schedule()
        assert (plain["overlap"], plain["max_parallel_runs"]) == ("skip", 3)
        # Nothing limits how many tasks run, so a parallel schedule's runs all start: there is nothing to warn of.
        status, made = await env.call("POST", f"/api/projects/{env.project_id}/schedules", {
            "name": "parallel", "cron": "*/5 * * * *", "requirement": "r", "overlap": "parallel",
            "max_parallel_runs": 10})
        assert status == 201 and made["schedule"]["max_parallel_runs"] == 10 and set(made) == {"schedule"}
        fixed = await env.ok("PUT", f"/api/schedules/{made['schedule']['schedule_id']}", {"max_parallel_runs": 2})
        assert fixed["schedule"]["max_parallel_runs"] == 2 and set(fixed) == {"schedule"}
        listed = (await env.ok("GET", f"/api/projects/{env.project_id}/schedules"))["limits"]
        assert (listed["max_parallel_runs_limit"], listed["concurrency_max"]) == (10, 64)
        assert "planner_limit" not in listed

    _run(test, monkeypatch, tmp_path)


@pytest.mark.parametrize(("body", "error"), [
    ({"gate_timeout_seconds": 601}, "gate_timeout_seconds must be"),
    ({"gate_command": "x" * 2001}, "at most 2000 characters"),
    ({"daily_budget_usd": 0}, "daily_budget_usd must be"),
    ({"notify": "sometimes"}, "notify must be one of failures, alerts, always, never"),
    ({"notify_target": {"channel": "telegram", "chat": "999"}}, "is not one the channel allows"),
    ({"notify_target": "telegram"}, "notify_target must be"),
])
def test_bad_s2_fields_are_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: dict[str, Any],
                                   error: str) -> None:
    async def test(env: _Env) -> None:
        await _allow_telegram(env)
        status, payload = await env.call("POST", f"/api/projects/{env.project_id}/schedules", {
            "name": "n", "cron": "*/5 * * * *", "requirement": "r", **body})
        assert status == 400 and error in payload["error"], payload
        limits = (await env.ok("GET", f"/api/projects/{env.project_id}/schedules"))["limits"]
        assert limits["notify_targets"] == [{"channel": "telegram", "chat": TELEGRAM_CHAT,
                                             "label": f"Telegram · {TELEGRAM_CHAT}", "channel_enabled": False}]

    _run(test, monkeypatch, tmp_path)


def test_the_dashboard_summary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.schedules import schedule_overview

    async def test(env: _Env) -> None:
        _setup(env)
        healthy = (await env.enabled(name="healthy", gate_command="true"))["schedule_id"]
        failing = (await env.enabled(name="failing", gate_command="exit 1"))["schedule_id"]
        await env.schedule(name="off")
        since = env.clock.now - timedelta(minutes=1)
        await _fire(env, healthy)
        for _ in range(3):
            await _fire(env, failing)
        await env.scheduler.idle()

        summary = await schedule_overview(since)
        assert (summary["total"], summary["enabled"]) == (3, 2)
        assert [item["name"] for item in summary["next"]] in (["failing", "healthy"], ["healthy", "failing"])
        [item] = summary["failing"]
        assert (item["name"], item["consecutive_failures"], item["last_status"]) == ("failing", 3, "gate_failed")
        assert item["failures_since"] is not None and item["project_name"] == "alerts"
        assert summary["skipped_today"] == {"skipped_gate": 1, "skipped_overlap": 0, "skipped_missed": 0,
                                            "skipped_budget": 0}

    _run(test, monkeypatch, tmp_path)
