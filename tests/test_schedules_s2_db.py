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
from tests.test_schedules_db import DSN, _Env, _run

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


# Noon in Los Angeles, where the scheduler's day (``_setup``) starts at midnight: far from a day's end.
NOON = datetime(2030, 1, 15, 20, 0, tzinfo=timezone.utc)
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


@pytest.mark.parametrize(("body", "error"), [
    ({"gate_timeout_seconds": 601}, "gate_timeout_seconds must be"),
    ({"gate_command": "x" * 2001}, "at most 2000 characters"),
    ({"daily_budget_usd": 0}, "daily_budget_usd must be"),
    ({"notify": "sometimes"}, "notify must be one of failures, always, never"),
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
