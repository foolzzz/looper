"""Schedule alert windows, streaks and command messages on a disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from looper.agents import cron, schedule_notify, schedules
from looper.db import schedules as db_schedules
from tests.db_support import DSN
from tests.schedule_support import UTC, ScheduleEnv, run_scheduled, with_sender

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
LA = cron.zone("America/Los_Angeles")


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 1, day, hour, minute, tzinfo=LA).astimezone(UTC)


async def configured(env: ScheduleEnv, **fields: Any) -> str:
    async with env.pool.acquire() as conn, conn.transaction():
        sid = await db_schedules.create_schedule(conn, project_id=env.project_id, profiles={}, fields={
            "name": "daily", "kind": "command", "command": "printf failure; exit 1", "requirement": "",
            "cron": "30 5 * * *", "timezone": "America/Los_Angeles", "run_timeout_mode": "none",
            "notify": "failures", "notify_channel": "telegram", "notify_chat": "stand-in", **fields,
        })
        await db_schedules.update_schedule(conn, sid, {"enabled": True, "enabled_at": env.clock.now})
    return sid


async def notices(env: ScheduleEnv, sid: str) -> list[dict[str, Any]]:
    return list(reversed(await db_schedules.list_notifications(sid, limit=100, pool=env.pool)))


async def ended_command(env: ScheduleEnv, sid: str, *, manual: bool = True) -> dict[str, Any]:
    if manual:
        run_id = await env.scheduler.run_now(sid)
    else:
        await env.scheduler.tick()
        run_id = await env.pool.fetchval("SELECT id FROM schedule_runs WHERE schedule_id=$1", sid)
    # Reap the real detached runner before settlement and before any assertions.
    process = env.scheduler._commands._owned[run_id]
    await asyncio.to_thread(process.wait, timeout=10)
    # The runner uses wall time in its own process; inject the scheduler's clock at its file boundary.
    exit_file = env.runtime_root / ".local" / "command-runs" / run_id / "exit.json"
    record = json.loads(exit_file.read_text())
    shift = env.clock.now - datetime.fromisoformat(record["ended_at"])
    for key in ("started_at", "ended_at"):
        if record[key]:
            record[key] = (datetime.fromisoformat(record[key]) + shift).isoformat()
    exit_file.write_text(json.dumps(record))
    await env.scheduler.tick()
    await env.scheduler.idle()
    return await db_schedules.get_run(run_id)


def test_daily_command_deadline_alert_and_manual_recovery(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sender = with_sender(env, dashboard_url="https://looper.example")
        env.clock.now = local(15, 0)
        sid = await configured(env, success_deadline="08:00")
        env.clock.now = local(15, 5, 30)
        await env.pool.execute("UPDATE schedules SET next_fire_at=$2 WHERE id=$1", sid, env.clock.now)
        run = await ended_command(env, sid, manual=False)
        assert (run["status"], run["exit_code"]) == ("failed", 1)
        [notice] = await notices(env, sid)
        assert notice["kind"] == "run"
        assert "退出码 1" in notice["text"] and "日志末尾：\n```\nfailure\n```" in notice["text"]
        assert notice["text"].endswith(f"/schedules/{sid}/runs/{run['id']}")
        env.clock.now = local(15, 8) + timedelta(seconds=15)
        for _ in range(3):
            await env.scheduler.tick()
        assert [n["kind"] for n in await notices(env, sid)] == ["run", "deadline_alert"]
        await env.pool.execute("UPDATE schedules SET command='true' WHERE id=$1", sid)
        assert (await ended_command(env, sid))["status"] == "succeeded"
        assert [n["kind"] for n in await notices(env, sid)] == ["run", "deadline_alert", "deadline_recovered"]
        assert len(sender.sent) == 3
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("weekday_only,enabled_day,enabled_hour,check_day,expected", [
    (True, 18, 0, 19, False),  # Saturday has no scheduled fire in its deadline window.
    (False, 15, 9, 15, False),
    (False, 15, 9, 16, True),
])
def test_deadline_only_covers_fires_after_enable(monkeypatch, tmp_path, weekday_only, enabled_day,
                                                enabled_hour, check_day, expected):
    async def test(env: ScheduleEnv):
        with_sender(env)
        env.clock.now = local(enabled_day, enabled_hour)
        sid = await configured(env, success_deadline="08:00", cron="30 5 * * 1-5" if weekday_only else "30 5 * * *")
        await env.scheduler._notifier.check_deadlines(local(check_day, 8))
        assert [n["kind"] for n in await notices(env, sid)] == (["deadline_alert"] if expected else [])
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("status", ["running", "failed", "skipped_gate"])
def test_deadline_describes_open_or_last_run_and_accepts_gate_success(monkeypatch, tmp_path, status):
    async def test(env: ScheduleEnv):
        with_sender(env)
        env.clock.now = local(15, 0)
        sid = await configured(env, success_deadline="08:00", kind="agent" if status == "skipped_gate" else "command",
                               command=None if status == "skipped_gate" else "exit 1")
        await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=local(15, 5, 30),
                                      fired_at=local(15, 5, 30), trigger="scheduled", status=status,
                                      error="example failure" if status == "failed" else None)
        if status == "skipped_gate":
            await db_schedules.count_outcome(env.pool, sid, status, local(15, 5, 30))
        await env.scheduler._notifier.check_deadlines(local(15, 8))
        rows = await notices(env, sid)
        if status == "skipped_gate":
            assert rows == []
        else:
            [notice] = rows
            assert ("仍在运行中" if status == "running" else "失败 · example failure") in notice["text"]
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("kind,threshold", [(k, t) for k in ("agent", "command") for t in (None, 1)])
def test_failure_threshold_defaults_and_override(monkeypatch, tmp_path, kind, threshold):
    async def test(env: ScheduleEnv):
        with_sender(env)
        sid = await configured(env, kind=kind, command=None if kind == "agent" else "exit 1",
                               alert_failures_after=threshold, notify="alerts")
        for n in range(1, 4):
            run_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                                   trigger="manual", status="failed", finished_at=env.clock.now)
            await db_schedules.count_outcome(env.pool, sid, "failed", env.clock.now)
            await asyncio.gather(*(env.scheduler._notifier.run_ended(run_id) for _ in range(2)))
            assert len(await notices(env, sid)) == int(n >= (threshold or 3))
    run_scheduled(test, monkeypatch, tmp_path)


def test_stale_window_exact_boundary(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        with_sender(env)
        env.clock.now = local(15, 0)
        sid = await configured(env, alert_stale_after_seconds=600)
        await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=local(15, 5, 30),
                                      trigger="scheduled", status="failed")
        await env.scheduler._notifier.check_stale(local(15, 5, 30) + timedelta(seconds=599))
        assert await notices(env, sid) == []
        await env.scheduler._notifier.check_stale(local(15, 5, 40))
        assert [n["kind"] for n in await notices(env, sid)] == ["stale_alert"]
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("kind,overlap,notify", [
    ("command", "skip", "alerts"), ("command", "parallel", "alerts"),
    ("command", "skip", "never"), ("agent", "skip", "failures"),
])
def test_overlap_streak_alert_recovers_on_started_run(monkeypatch, tmp_path, kind, overlap, notify):
    async def test(env: ScheduleEnv):
        sender = with_sender(env)
        env.clock.now = local(15, 0)
        if kind == "agent":
            # Agent tasks use their real task state to decide whether another run overlaps.
            sid = (await env.enabled())["schedule_id"]
            first = await db_schedules.get_run(await env.scheduler.run_now(sid))
        else:
            sid = await configured(env, overlap=overlap, max_parallel_runs=1, command="true",
                                   alert_overlap_after=2, notify=notify)
            run_id = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                                   fired_at=env.clock.now, trigger="scheduled", status="running")
            first = {"run_id": run_id}
        for n in range(1, (20 if kind == "agent" else 3) + 1):
            await env.scheduler.run_now(sid)
            await env.scheduler.idle()
            assert await env.pool.fetchval("SELECT consecutive_overlaps FROM schedules WHERE id=$1", sid) == n
        if kind == "agent":
            assert await notices(env, sid) == [] and sender.sent == []
            await env.end_task(first["task_id"], "completed")
            await env.scheduler.run_now(sid)
        else:
            [notice] = await notices(env, sid)
            assert notice["kind"] == "overlap_alert"
            assert "已连续 2 次" in notice["text"] and notice["text"].endswith(f"/runs/{first['run_id']}")
            await db_schedules.finish_command_run(env.pool, first["run_id"], status="succeeded", at=env.clock.now,
                                                   error=None)
            await ended_command(env, sid)
            rows = await notices(env, sid)
            assert [n["kind"] for n in rows] == ["overlap_alert", "overlap_recovered"]
            assert all(n["status"] == ("dashboard" if notify == "never" else "sent") for n in rows)
            assert len(sender.sent) == (0 if notify == "never" else 2)
        assert await env.pool.fetchval("SELECT consecutive_overlaps FROM schedules WHERE id=$1", sid) == 0
    run_scheduled(test, monkeypatch, tmp_path)


def test_env_file_values_are_redacted_in_command_notifications(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        sender = with_sender(env)
        secret = "private-command-value-793165"
        env_file = tmp_path / "command.env"
        env_file.write_text(f"TOKEN={secret}\n")
        sid = await configured(env, command='printf "%s" "$TOKEN"; exit 1', env_file=str(env_file),
                               alert_failures_after=1)
        await ended_command(env, sid)
        rows = await notices(env, sid)
        assert [n["kind"] for n in rows] == ["run", "failures_alert"]
        assert len(sender.sent) == 2
        assert all(secret not in text for _, _, text in sender.sent)
        assert all(secret not in row["text"] for row in rows)
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("notify", ["alerts", "never"])
def test_deadline_alert_is_atomic_dashboard_visible_and_suppressed_when_never(monkeypatch, tmp_path, notify):
    async def test(env: ScheduleEnv):
        sender = with_sender(env)
        env.clock.now = local(15, 0)
        sid = await configured(env, success_deadline="08:00", notify=notify)
        env.clock.now = local(15, 8)
        await asyncio.gather(*(env.scheduler._notifier.check_deadlines(env.clock.now) for _ in range(2)))
        [notice] = await notices(env, sid)
        assert notice["status"] == ("dashboard" if notify == "never" else "sent")
        assert len(sender.sent) == (0 if notify == "never" else 1)
        overview = await schedules.schedule_overview(env.clock.now)
        [item] = overview["failing"]
        assert item["schedule_id"] == sid and item["deadline_since"] == env.clock.now
        assert item["overlap_since"] is None
    run_scheduled(test, monkeypatch, tmp_path)


def test_deadline_compare_and_set_rejects_a_success_after_the_read(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        with_sender(env)
        env.clock.now = local(15, 0)
        sid = await configured(env, success_deadline="08:00")
        start = db_schedules.start_deadline_alert

        async def success_before_start(conn, schedule_id, at, *, last_ok_at):
            await db_schedules.count_outcome(env.pool, sid, "succeeded", local(15, 8))
            return await start(conn, schedule_id, at, last_ok_at=last_ok_at)

        monkeypatch.setattr(db_schedules, "start_deadline_alert", success_before_start)
        await env.scheduler._notifier.check_deadlines(local(15, 8))
        assert await notices(env, sid) == []
        assert await env.pool.fetchval("SELECT deadline_alert_at FROM schedules WHERE id=$1", sid) is None
    run_scheduled(test, monkeypatch, tmp_path)


def test_changing_deadline_starts_a_new_window(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        with_sender(env)
        env.clock.now = local(15, 0)
        sid = (await env.enabled(cron="30 5 * * *"))["schedule_id"]
        env.clock.now = local(15, 9)
        with pytest.raises(schedules.RequestError, match="success_deadline"):
            await schedules.update_schedule(sid, {"success_deadline": "24:00"}, now=env.clock.now)
        await schedules.update_schedule(sid, {"success_deadline": "08:00"}, now=env.clock.now)
        row = await db_schedules.get_schedule(sid)
        assert row["success_deadline"] == "08:00" and row["rule_changed_at"] == env.clock.now
        await env.scheduler._notifier.check_deadlines(env.clock.now)
        assert await notices(env, sid) == []
        await env.scheduler._notifier.check_deadlines(local(16, 8))
        assert [n["kind"] for n in await notices(env, sid)] == ["deadline_alert"]
        await schedules.update_schedule(sid, {"enabled": False}, now=local(16, 9))
        await env.pool.execute("UPDATE schedules SET deadline_alert_at=NULL WHERE id=$1", sid)
        await env.scheduler._notifier.check_deadlines(local(17, 8))
        assert len(await notices(env, sid)) == 1
    run_scheduled(test, monkeypatch, tmp_path)


def test_deadline_in_daylight_saving_gap_uses_the_next_valid_instant(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        with_sender(env)
        env.clock.now = datetime(2030, 3, 10, tzinfo=LA).astimezone(UTC)
        sid = await configured(env, success_deadline="02:30", cron="30 1 * * *")
        due = datetime(2030, 3, 10, 3, 0, tzinfo=LA).astimezone(UTC)
        await env.scheduler._notifier.check_deadlines(due - timedelta(seconds=1))
        assert await notices(env, sid) == []
        await env.scheduler._notifier.check_deadlines(due)
        [notice] = await notices(env, sid)
        assert notice["kind"] == "deadline_alert" and "03:00 PDT" in notice["text"]
    run_scheduled(test, monkeypatch, tmp_path)


def test_overlap_alert_keeps_the_triggering_run_and_count_during_recovery(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        with_sender(env)
        sid = await configured(env, command="true", alert_overlap_after=1, notify="alerts")
        old = await db_schedules.insert_run(env.pool, schedule_id=sid, scheduled_for=env.clock.now,
                                            fired_at=env.clock.now, trigger="scheduled", status="running")
        entered, release = asyncio.Event(), asyncio.Event()
        send = env.scheduler._notifier.overlap_alert

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            await send(*args, **kwargs)

        monkeypatch.setattr(env.scheduler._notifier, "overlap_alert", delayed)
        await env.scheduler.run_now(sid)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await db_schedules.finish_command_run(env.pool, old, status="succeeded", at=env.clock.now, error=None)
        # Start a new run while delivery of the old alert is deliberately held.
        new = await env.scheduler.run_now(sid)
        await asyncio.to_thread(env.scheduler._commands._owned[new].wait, timeout=10)
        release.set()
        await env.scheduler.idle()
        rows = await notices(env, sid)
        alert = next(row for row in rows if row["kind"] == "overlap_alert")
        assert "已连续 1 次" in alert["text"] and alert["text"].endswith(f"/runs/{old}")
        assert len([row for row in rows if row["kind"] == "overlap_recovered"]) == 1
        await env.scheduler.tick()
    run_scheduled(test, monkeypatch, tmp_path)


def test_gated_run_resets_overlaps_when_the_check_starts(monkeypatch, tmp_path):
    async def test(env: ScheduleEnv):
        with_sender(env)
        sid = (await env.enabled(gate_command="true", notify="alerts"))["schedule_id"]
        await env.pool.execute("UPDATE schedules SET consecutive_overlaps=2, overlap_alert_at=$2 WHERE id=$1",
                               sid, env.clock.now)
        run_id = await env.scheduler.run_now(sid)
        assert await env.pool.fetchval("SELECT consecutive_overlaps FROM schedules WHERE id=$1", sid) == 0
        await env.scheduler.idle()
        assert (await db_schedules.get_run(run_id))["status"] == "skipped_gate"
        assert [n["kind"] for n in await notices(env, sid)] == ["overlap_recovered"]
    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("kind,threshold,effective", [
    ("agent", None, 0), ("command", None, 10), ("command", 0, 0),
])
def test_overlap_policy_respects_disabled_and_type_defaults(kind, threshold, effective):
    policy = schedule_notify.alert_policy({"kind": kind, "alert_overlap_after": threshold})
    assert policy.overlap_after == effective
    assert policy.failures_after == 3 and policy.stale_after is None


@pytest.mark.parametrize("status,label,seconds,elapsed", [
    ("succeeded", "成功", 59, "59 秒"), ("failed", "失败", 60, "1 分 0 秒"),
    ("timed_out", "超时被停止", 3599, "59 分 59 秒"), ("interrupted", "中断", 3600, "1 小时 0 分"),
])
def test_command_message_status_duration_scheduled_time_and_safe_log_fence(status, label, seconds, elapsed):
    assert schedule_notify.wants_run_message("always", status)
    at = local(15, 5, 30)
    text = schedule_notify.run_text({"id": "s", "project_id": "p", "name": "daily", "kind": "command",
                                     "timezone": "America/Los_Angeles"}, "project", {
        "id": "r", "status": status, "scheduled_for": at, "fired_at": at + timedelta(minutes=5),
        "started_at": at, "finished_at": at + timedelta(seconds=seconds), "trigger": "catch_up", "missed": 2,
        "exit_code": 7 if status == "failed" else None, "exit_signal": 9 if status == "interrupted" else None,
        "log_tail": "prefix-" * 300 + "```hello````", "error": "e" * 350,
    })
    assert f"05:30 PST 的运行：{label}" in text
    assert f"耗时 {elapsed} · 补跑，代替错过的 2 次" in text
    assert ("（退出码 7）" in text) == (status == "failed")
    assert ("（信号 9）" in text) == (status == "interrupted")
    assert "日志末尾：\n`````\n" in text and "`````\n/#projects/p/schedules/s/runs/r" in text
    assert "e" * 301 not in text
    assert "prefix-" * 300 not in text
