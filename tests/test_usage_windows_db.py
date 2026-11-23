"""Usage-window holds on a disposable database: schedule deferral, background callers, and one notice per signal.

Synthetic providers and windows only. No model is started: a held outbound review never reaches the stand-in,
and a held leads check stores every item unchecked.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import asyncpg
import pytest

from tests.db_support import DSN, run_db
from tests.fake_claude import fake_claude
from tests.schedule_support import NOON, ScheduleEnv, run_scheduled
from tests.subscription_support import cursor, lark_targets, started

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

BUILTIN = "prov-claude-subscription"
UTC = timezone.utc


async def _quiet() -> None:
    from looper.agents import cost_guard, cost_guard_settings as settings, usage_windows

    try:
        await settings.update({"mode": "off"})
        await cost_guard.refresh()
    except Exception:
        pass
    usage_windows.clear_cache()
    usage_windows.configure(None)


async def _enforce(pool, *, signals: bool = True, clock=None, day_start=None) -> None:
    from looper.agents import cost_guard, cost_guard_settings as settings
    from looper.db import system_settings

    await system_settings.put_settings(
        {"engine.limit_signals.enabled": "true" if signals else None}, pool=pool)
    await cost_guard.initialize(day_start=day_start or (lambda now: now), pool=pool)
    guard = cost_guard.current()
    if clock is not None:
        guard.clock = clock
    await settings.update({"mode": "enforce"}, pool=pool)
    await cost_guard.refresh()
    guard = cost_guard.current()
    if clock is not None:
        guard.clock = clock
        guard.day = (day_start or (lambda now: now))(clock())


async def _window(pool, *, status="warning", utilization=0.95, resets_at, observed_at, name="five_hour",
                  provider=BUILTIN) -> None:
    from looper.agents import usage_windows
    from looper.db import provider_limits

    fields = {"status": status, "utilization": utilization, "source": "claude_stream", "observed_at": observed_at,
              "resets_at": resets_at}
    await provider_limits.upsert(provider, name, fields, pool=pool)
    usage_windows.clear_cache()


async def _failure(pool, *, at: datetime, resets_at: datetime | None = None, cause: str = "rate_limit",
                   error: str = "retry at 2099-01-01T00:00:00Z", provider=BUILTIN) -> None:
    from looper.agents import usage_windows

    metadata: dict[str, Any] = {"profile": {"provider": {"id": provider}}, "failure": {}}
    if resets_at is not None:
        metadata["failure"]["resets_at"] = resets_at.isoformat()
    await pool.execute(
        "INSERT INTO llm_records (agent_id, status, metadata, failure_class, failure_cause, created_at, "
        "finished_at, error) VALUES ('worker-synthetic', 'failed', $1::jsonb, 'quota', $2, $3, $3, $4)",
        json.dumps(metadata), cause, at, error)
    usage_windows.clear_cache()


def test_high_utilization_defers_a_firing_until_its_reset_then_admits_it_again(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import usage_windows

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            sid = (await env.enabled())["schedule_id"]
            await env.due(sid)
            resets = env.clock.now + timedelta(seconds=30)
            await _failure(env.pool, at=env.clock.now - timedelta(hours=1), cause="usage_limit",
                           error="already failed; do not replay this step")
            before = await env.pool.fetchval("SELECT count(*) FROM llm_records WHERE failure_class = 'quota'")
            await _window(env.pool, utilization=0.95, resets_at=resets, observed_at=env.clock.now)
            next_at = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
            await env.scheduler.tick()
            assert await env.runs(sid) == [] and env.bus.of("task_start") == []
            row = await env.pool.fetchrow("SELECT last_fired_at, next_fire_at FROM schedules WHERE id = $1", sid)
            assert row["last_fired_at"] is None and row["next_fire_at"] == next_at
            view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
            assert view["usage_hold"]["reason"] == "high_utilization"
            assert datetime.fromisoformat(view["usage_hold"]["until"]) == resets
            assert await env.pool.fetchval("SELECT count(*) FROM llm_records WHERE failure_class = 'quota'") == before

            env.clock.now = resets + timedelta(seconds=1)
            usage_windows.clear_cache()
            await env.scheduler.tick()
            assert env.bus.of("task_start") and len(await env.runs(sid)) == 1
            resumed = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
            assert "usage_hold" not in resumed
            assert await env.pool.fetchval("SELECT count(*) FROM llm_records WHERE failure_class = 'quota'") == before
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_after_the_hold_the_firing_is_checked_against_the_daily_budget(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import usage_windows

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            sid = (await env.enabled(daily_budget_usd=2))["schedule_id"]
            await env.due(sid)
            await env.pool.execute(
                "INSERT INTO schedule_runs (id, schedule_id, scheduled_for, fired_at, trigger, status, cost_usd, "
                "finished_at) VALUES ('run-spent', $1, $2, $2, 'manual', 'completed', 1, $2)",
                sid, env.clock.now)
            resets = env.clock.now + timedelta(seconds=30)
            await _window(env.pool, utilization=0.95, resets_at=resets, observed_at=env.clock.now)
            await env.scheduler.tick()
            assert env.bus.of("task_start") == []
            assert [run["status"] for run in await env.runs(sid)] == ["completed"]
            # Spend crosses the cap during the wait; the resumed firing must check the legacy total again.
            await env.pool.execute("UPDATE schedule_runs SET cost_usd=3 WHERE id='run-spent'")
            env.clock.now = resets + timedelta(seconds=1)
            usage_windows.clear_cache()
            await env.scheduler.tick()
            assert env.bus.of("task_start") == []
            assert {run["status"] for run in await env.runs(sid)} == {"completed", "skipped_budget"}
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_094_and_a_past_reset_do_not_defer_a_firing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            low = (await env.enabled())["schedule_id"]
            past = (await env.enabled())["schedule_id"]
            await env.due(low)
            await env.pool.execute(
                "UPDATE schedules SET next_fire_at = (SELECT next_fire_at FROM schedules WHERE id = $1) WHERE id = $2",
                low, past)
            await _window(env.pool, utilization=0.94, resets_at=env.clock.now + timedelta(hours=1),
                          observed_at=env.clock.now, name="five_hour")
            await _window(env.pool, status="rejected", utilization=1, resets_at=env.clock.now - timedelta(minutes=1),
                          observed_at=env.clock.now - timedelta(hours=1), name="seven_day")
            await env.scheduler.tick()
            started = env.bus.of("task_start")
            assert len(started) == 2 and len(set(started)) == 2
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_rejected_holds_until_the_reset_and_notifies_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import gate_service, usage_windows

        sent: list[tuple[str, str, str]] = []
        locked: list[str] = []

        async def sender(channel, chat, text, **_kwargs):
            try:
                async with env.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '500ms'")
                        await conn.fetchrow("SELECT id FROM schedules WHERE id = $1 FOR UPDATE", sid)
                locked.append("ok")
            except asyncpg.exceptions.LockNotAvailableError:
                locked.append("locked")
            sent.append((channel, chat, text))
            return True, None

        async def target():
            return SimpleNamespace(channel_id="channel-lark", target=SimpleNamespace(id="ou_synthetic"))

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", target)
            sid = (await env.enabled())["schedule_id"]
            await env.due(sid)
            resets = env.clock.now + timedelta(seconds=30)
            await _window(env.pool, status="rejected", utilization=1, resets_at=resets, observed_at=env.clock.now)
            await env.scheduler.tick()
            await env.scheduler.tick()
            assert await env.runs(sid) == []
            assert len(sent) == 1 and sent[0][0] == "channel-lark" and "后台工作会等待" in sent[0][2]
            assert BUILTIN in sent[0][2] and "重置时间未知" not in sent[0][2]
            assert locked == ["ok"]
            started = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
            assert started["run"]["trigger"] == "manual"
            waiting = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
            assert waiting["usage_hold"]["reason"] == "rejected"
            assert datetime.fromisoformat(waiting["usage_hold"]["until"]) == resets
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_known_high_utilization_reset_sends_no_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import gate_service, usage_windows

        sent: list[str] = []

        async def sender(_channel, _chat, text, **_kwargs):
            sent.append(text)
            return True, None

        async def target():
            return SimpleNamespace(channel_id="channel-lark", target=SimpleNamespace(id="ou_synthetic"))

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", target)
            sid = (await env.enabled())["schedule_id"]
            await env.due(sid)
            await _window(env.pool, utilization=0.95, resets_at=env.clock.now + timedelta(seconds=30),
                          observed_at=env.clock.now)
            await env.scheduler.tick()
            assert await env.runs(sid) == [] and sent == []
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_observe_does_not_defer_or_notify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import cost_guard, cost_guard_settings as settings, gate_service, usage_windows

        sent: list[str] = []

        async def sender(*_args, **_kwargs):
            sent.append("sent")
            return True, None

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            await settings.update({"mode": "observe"}, pool=env.pool)
            await cost_guard.refresh()
            cost_guard.current().clock = env.clock
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", AsyncMockTarget())
            sid = (await env.enabled())["schedule_id"]
            await env.due(sid)
            await _window(env.pool, utilization=0.95, resets_at=env.clock.now + timedelta(hours=1),
                          observed_at=env.clock.now)
            await env.scheduler.tick()
            assert env.bus.of("task_start") and sent == []
            view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
            assert "usage_hold" not in view
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


class AsyncMockTarget:
    async def __call__(self):
        return SimpleNamespace(channel_id="channel-lark", target=SimpleNamespace(id="ou_synthetic"))


def test_off_does_not_read_windows_and_a_firing_still_starts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.db import provider_limits

        calls: list[Any] = []
        real = provider_limits.windows

        async def wrapped(*args, **kwargs):
            calls.append(args)
            return await real(*args, **kwargs)

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            from looper.agents import cost_guard, cost_guard_settings as settings

            await settings.update({"mode": "off"}, pool=env.pool)
            await cost_guard.refresh()
            monkeypatch.setattr(provider_limits, "windows", wrapped)
            sid = (await env.enabled())["schedule_id"]
            await env.due(sid)
            await _window(env.pool, status="rejected", utilization=1, resets_at=env.clock.now + timedelta(hours=1),
                          observed_at=env.clock.now)
            calls.clear()
            await env.scheduler.tick()
            assert env.bus.of("task_start") and calls == []
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_signals_off_ignores_windows_and_a_quota_failure_still_holds(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.db import provider_limits

        calls: list[Any] = []
        real = provider_limits.windows

        async def wrapped(*args, **kwargs):
            calls.append(args)
            return await real(*args, **kwargs)

        try:
            await _enforce(env.pool, signals=False, clock=env.clock, day_start=env.scheduler.day_start)
            monkeypatch.setattr(provider_limits, "windows", wrapped)
            ignored = (await env.enabled())["schedule_id"]
            await env.due(ignored)
            await _window(env.pool, status="rejected", utilization=1, resets_at=env.clock.now + timedelta(hours=1),
                          observed_at=env.clock.now)
            calls.clear()
            await env.scheduler.tick()
            assert env.bus.of("task_start") and calls == []

            held = (await env.enabled())["schedule_id"]
            await env.due(held)
            reset = env.clock.now + timedelta(minutes=20)
            await _failure(env.pool, at=env.clock.now, resets_at=reset, cause="usage_limit",
                           error="the text says 2099-01-01, which is not the reset")
            await env.scheduler.tick()
            view = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": held}))["schedule"]
            assert view["usage_hold"]["reason"] == "quota_failure"
            assert datetime.fromisoformat(view["usage_hold"]["until"]) == reset
            assert "2099" not in view["usage_hold"]["until"]
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_cooldown_is_five_minutes_from_the_stored_time_and_a_restart_does_not_extend_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool) -> None:
        from looper.agents import usage_windows

        try:
            await _enforce(pool, signals=True)
            observed = datetime.now(UTC) - timedelta(minutes=1)
            await _window(pool, status="rejected", utilization=0.2, resets_at=None, observed_at=observed,
                          name="session")
            now = observed + timedelta(minutes=2)
            first = await usage_windows.hold_for(BUILTIN, background=False, now=now, pool=pool)
            assert first is not None and first.until == observed + timedelta(minutes=5)
            assert first.reset_known is False and first.reason is usage_windows.HoldReason.REJECTED
            for _ in range(10):
                again = await usage_windows.hold_for(BUILTIN, background=False, now=now + timedelta(seconds=20),
                                                     pool=pool)
                assert again.until == first.until
            usage_windows.clear_cache()
            restarted = await usage_windows.hold_for(BUILTIN, background=False, now=now, pool=pool)
            assert restarted.until == first.until
            assert await usage_windows.hold_for(BUILTIN, background=False, now=first.until, pool=pool) is None

            ended = datetime.now(UTC) - timedelta(minutes=1)
            other = "prov-cooldown-only"
            await pool.execute(
                "INSERT INTO llm_providers (id, name, vendor_type, auth) VALUES ($1, 'Cooldown', 'custom', 'api_key')",
                other)
            await _failure(pool, at=ended, cause="rate_limit", provider=other)
            cooled = await usage_windows.hold_for(other, background=True, now=ended + timedelta(minutes=1), pool=pool)
            assert cooled.reason is usage_windows.HoldReason.QUOTA_COOLDOWN
            assert cooled.until == ended + timedelta(minutes=5) and cooled.reset_known is False
            usage_windows.clear_cache()
            assert (await usage_windows.hold_for(other, background=True, now=ended + timedelta(minutes=4),
                                                pool=pool)).until == cooled.until
        finally:
            await _quiet()

    run_db(test, monkeypatch)


def test_subscription_cursor_stays_and_outbound_blocks_while_leads_pass_through(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import extraction, leads_check, outbound
        from looper.agents.outbound import Action, ActionLevel, Asker, Origin, Who
        from looper.agents.serve import seed_role_configs

        try:
            await seed_role_configs(env.pool)
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            await lark_targets(env.pool)
            sid = await started(env)
            before = await cursor(env, sid)
            await env.due(sid)
            resets = env.clock.now + timedelta(seconds=30)
            await _window(env.pool, status="rejected", utilization=1, resets_at=resets, observed_at=env.clock.now)
            await env.scheduler.tick()
            assert await cursor(env, sid) == before
            assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 0

            # The outbound and leads paths use the wall clock, so the same account stays rejected past it.
            wall = datetime.now(UTC) + timedelta(minutes=30)
            await _window(env.pool, status="rejected", utilization=1, resets_at=wall, observed_at=datetime.now(UTC),
                          name="seven_day")
            from looper.agents import cost_guard
            cost_guard.current().clock = lambda: datetime.now(UTC)
            seen = tmp_path / "outbound-seen.jsonl"
            monkeypatch.setenv("OUTBOUND_SEEN", str(seen))
            monkeypatch.setenv("OUTBOUND_REPLY", "{}")
            action = Action(tool="looper_lark", action="send", level=ActionLevel.EXTERNAL, target_kind="chat",
                            target="Build status", summary="Send a message", content="The nightly build is green.")
            review = await outbound.review(
                action, Asker(Who.OPERATOR, "lark", "Post the build status."),
                Origin("planner-task", outbound.Source.TOOL, task_id=None, channel="lark", chat="oc_build"),
                runtime_root=tmp_path / "rt", claude_bin=fake_claude(tmp_path / "claude", _STAND_IN))
            assert review.allowed is False and review.failure is outbound.Failure.UNAVAILABLE
            assert str(review.reason).startswith("usage window on hold until ")
            assert not seen.exists()

            items = [{"kind": "commitment", "summary": "李四今天下班前修好登录页", "detail": "", "confidence": "high",
                      "sources": [1], "window_sources": [1], "about": []}]
            untouched = json.loads(json.dumps(items))
            people = extraction.Participants({"ou_zhang": "张三"}, "ou_user")
            messages = {1: {"seq": 1, "external_id": "om_1", "chat_id": "oc_team", "chat_kind": "group",
                            "chat_name": "研发群", "parent_id": None, "sender_id": "ou_zhang", "sender_kind": "user",
                            "sender_name": "张三", "sent_at": NOON, "text": "登录页的报错谁能看一下？"}}
            outcome = await leads_check.check(
                "leads", items, messages=messages, participants=people, zone_name="Asia/Shanghai", text_max_chars=500,
                types={}, owner="sub-synthetic", reference={"window": "w"}, budget_usd=0.5, timeout_s=30,
                runtime_root=tmp_path / "leads", claude_bin=str(tmp_path / "claude"))
            assert outcome.checked is None and items == untouched
            assert outcome.detail.startswith("failed: usage window on hold until ")
            assert outcome.counts["check_failed"] is True
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("catch_up", ["skip", "once"])
def test_a_hold_longer_than_grace_stays_due_and_then_fires_on_time(
        catch_up: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import cost_guard

        def held_rows() -> list[dict[str, Any]]:
            return [row for row in cost_guard.current().snapshot()["recent"]
                    if row.get("decision") == "hold" or row.get("would_hold_until")]

        def waits() -> list[logging.LogRecord]:
            return [record for record in caplog.records if "waits for a usage window" in record.getMessage()]

        try:
            caplog.set_level(logging.INFO)
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            sid = (await env.enabled(cron="0 12 * * *", timezone="America/Los_Angeles", catch_up=catch_up))["schedule_id"]
            await env.pool.execute(
                "UPDATE schedules SET next_fire_at = $2, last_fired_at = NULL WHERE id = $1", sid, NOON)
            resets = NOON + timedelta(minutes=10)
            env.clock.now = NOON + timedelta(minutes=3)
            await _window(env.pool, status="rejected", utilization=1, resets_at=resets, observed_at=NOON)
            next_at = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
            await env.scheduler.tick()
            row = await env.pool.fetchrow("SELECT last_fired_at, next_fire_at FROM schedules WHERE id = $1", sid)
            assert await env.runs(sid) == []
            assert row["last_fired_at"] is None and row["next_fire_at"] == next_at
            assert len(held_rows()) == 1 and len(waits()) == 1

            env.clock.now += timedelta(seconds=20)
            await env.scheduler.tick()
            assert await env.runs(sid) == []
            assert len(held_rows()) == 1 and len(waits()) == 1
            still = await env.pool.fetchrow("SELECT last_fired_at, next_fire_at FROM schedules WHERE id = $1", sid)
            assert still["last_fired_at"] is None and still["next_fire_at"] == next_at

            extended = resets + timedelta(minutes=5)
            await _window(env.pool, status="rejected", utilization=1, resets_at=extended, observed_at=env.clock.now)
            env.clock.now += timedelta(seconds=20)
            await env.scheduler.tick()
            assert len(held_rows()) == 2 and len(waits()) == 2
            assert await env.runs(sid) == []

            # The cached window already ends at ``extended``. Clearing it would also drop the deferral note.
            env.clock.now = extended + timedelta(seconds=1)
            await env.scheduler.tick()
            runs = await env.runs(sid)
            started = [run for run in runs if run["trigger"] == "scheduled"]
            assert len(started) == 1 and started[0]["missed"] == 0
            assert all(run["trigger"] != "catch_up" and run["status"] != "skipped_missed" for run in runs)
            fired = await env.pool.fetchrow("SELECT last_fired_at, next_fire_at FROM schedules WHERE id = $1", sid)
            assert fired["last_fired_at"] == NOON and fired["next_fire_at"] != next_at
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_an_operator_run_now_starts_during_a_high_utilization_hold(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            fresh = (await env.enabled())["schedule_id"]
            resets = env.clock.now + timedelta(minutes=10)
            await _window(env.pool, utilization=0.95, resets_at=resets, observed_at=env.clock.now)
            started = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": fresh})
            assert started["run"]["trigger"] == "manual"
            alone = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": fresh}))["schedule"]
            assert "usage_hold" not in alone

            sid = (await env.enabled())["schedule_id"]
            await env.due(sid)
            await _window(env.pool, utilization=0.95, resets_at=resets, observed_at=env.clock.now)
            await env.scheduler.tick()
            assert await env.runs(sid) == []
            next_at = await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid)
            manual = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
            assert manual["run"]["trigger"] == "manual"
            waiting = (await env.ok("POST", "/api/v1/schedule_admin/get", {"schedule_id": sid}))["schedule"]
            assert waiting["usage_hold"]["reason"] == "high_utilization"
            assert await env.pool.fetchval("SELECT next_fire_at FROM schedules WHERE id = $1", sid) == next_at
            assert not any(run["status"] == "skipped_missed" for run in await env.runs(sid))
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_hold_notice_keeps_one_settings_row_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool) -> None:
        from looper.agents import gate_service, usage_windows

        sent: list[str] = []

        async def sender(_channel, _chat, text, **_kwargs):
            sent.append(text)
            return True, None

        async def target():
            return SimpleNamespace(channel_id="channel-lark", target=SimpleNamespace(id="ou_synthetic"))

        try:
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", target)
            day = datetime(2026, 1, 15, 20, tzinfo=UTC)
            provider = "prov-synthetic"
            await usage_windows.notify_hold(provider, usage_windows.Hold(
                day + timedelta(minutes=5), usage_windows.HoldReason.QUOTA_COOLDOWN, False, None))
            await usage_windows.notify_hold(provider, usage_windows.Hold(
                day + timedelta(minutes=6), usage_windows.HoldReason.QUOTA_COOLDOWN, False, None))
            await usage_windows.notify_hold(provider, usage_windows.Hold(
                day + timedelta(hours=1), usage_windows.HoldReason.REJECTED, True, "five_hour"))
            await usage_windows.notify_hold(provider, usage_windows.Hold(
                day + timedelta(hours=2), usage_windows.HoldReason.REJECTED, True, "five_hour"))
            assert len(sent) == 3
            rows = await pool.fetch(
                "SELECT key FROM system_settings WHERE key LIKE $1 ORDER BY key",
                "cost_guard_hold:prov-synthetic:%")
            assert [row["key"] for row in rows] == [
                "cost_guard_hold:prov-synthetic:failure",
                "cost_guard_hold:prov-synthetic:five_hour",
            ]
        finally:
            usage_windows.configure(None)

    run_db(test, monkeypatch)


def test_settings_get_lists_a_current_hold_and_omits_it_while_off(monkeypatch: pytest.MonkeyPatch) -> None:
    async def test(pool) -> None:
        from looper.agents import cost_guard, cost_guard_settings as settings
        from looper.agents.v1_domains import cost_guard as domain

        try:
            await _enforce(pool)
            await _window(pool, status="rejected", utilization=1, resets_at=datetime.now(UTC) + timedelta(minutes=20),
                          observed_at=datetime.now(UTC))
            view = await domain.run(None, domain.OPERATIONS[0], {}, None, None)
            assert view["mode"] == "enforce"
            assert view["holds"][0]["provider"] == BUILTIN and view["holds"][0]["reason"] == "rejected"
            await settings.update({"mode": "off"}, pool=pool)
            await cost_guard.refresh()
            off = await domain.run(None, domain.OPERATIONS[0], {}, None, None)
            assert "holds" not in off
        finally:
            await _quiet()

    run_db(test, monkeypatch)


def _hold_message(until: datetime) -> str:
    when = until.astimezone().strftime("%H:%M")
    return f"usage window on hold until {when} (rejected); the subscription runs when it lifts"


async def _stored_run(env: ScheduleEnv, schedule_id: str) -> dict[str, Any]:
    row = await env.pool.fetchrow(
        "SELECT trigger, status, error FROM schedule_runs WHERE schedule_id = $1", schedule_id)
    return dict(row)


def test_a_firing_sends_its_notice_once_after_its_commit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import usage_windows

        published: list[tuple[Any, Any]] = []
        a_queued = asyncio.Event()
        b_failed = asyncio.Event()
        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            a_id = (await env.enabled())["schedule_id"]
            b_id = (await env.enabled())["schedule_id"]
            await _window(env.pool, status="rejected", utilization=1,
                          resets_at=env.clock.now + timedelta(minutes=30), observed_at=env.clock.now)
            real_block = env.scheduler._usage_block
            real_notify = usage_windows.notify_hold

            async def block(schedule, now, notices=None):
                if schedule["id"] == b_id:
                    await asyncio.wait_for(a_queued.wait(), timeout=10)
                if notices is None:
                    held = await real_block(schedule, now)
                    pending = getattr(env.scheduler, "_hold_notices", None)
                else:
                    held = await real_block(schedule, now, notices)
                    pending = notices
                assert pending, "the firing queued no hold notice"
                if schedule["id"] == a_id:
                    a_queued.set()
                    await asyncio.wait_for(b_failed.wait(), timeout=10)
                else:
                    assert published == [], "a hold notice went out before the firing committed"
                    raise RuntimeError("firing rolled back after its notice was queued")
                return held

            async def notify(provider_id, hold):
                async with env.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '500ms'")
                        await conn.fetch(
                            "SELECT id FROM schedules WHERE id = ANY($1::text[]) FOR UPDATE", [a_id, b_id])
                assert b_failed.is_set()
                published.append((provider_id, hold))
                await real_notify(provider_id, hold)

            env.scheduler._usage_block = block
            monkeypatch.setattr(usage_windows, "notify_hold", notify)

            async def fire_a() -> None:
                with pytest.raises(usage_windows.ScheduleDeferred):
                    await env.scheduler.run_now(a_id)

            async def fire_b() -> None:
                try:
                    with pytest.raises(RuntimeError, match="firing rolled back after its notice was queued"):
                        await env.scheduler.run_now(b_id)
                finally:
                    b_failed.set()

            await asyncio.gather(fire_a(), fire_b())
            assert len(published) == 1 and published[0][0] == BUILTIN, (
                f"each firing publishes only its own notice after it commits, got {published!r}")
            assert await env.pool.fetchval(
                "SELECT count(*) FROM schedule_runs WHERE schedule_id = ANY($1::text[])", [a_id, b_id]) == 0
            assert await env.pool.fetchval(
                "SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard_hold:%'") == 1
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_rolled_back_firing_sends_no_notice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import usage_windows

        published: list[Any] = []
        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            sid = (await env.enabled())["schedule_id"]
            await _window(env.pool, status="rejected", utilization=1,
                          resets_at=env.clock.now + timedelta(minutes=30), observed_at=env.clock.now)
            real_block = env.scheduler._usage_block
            real_notify = usage_windows.notify_hold
            from looper.db import schedules as db_schedules

            real_lock = db_schedules.lock_schedule
            original = await env.pool.fetchval("SELECT requirement FROM schedules WHERE id = $1", sid)
            marker = f"{original} [committed]"

            async def lock(conn, schedule_id):
                row = await real_lock(conn, schedule_id)
                if row is not None and schedule_id == sid:
                    await conn.execute(
                        "UPDATE schedules SET requirement = $2 WHERE id = $1", schedule_id, marker)
                return row

            async def block(schedule, now, notices=None):
                if notices is None:
                    await real_block(schedule, now)
                    pending = getattr(env.scheduler, "_hold_notices", None)
                else:
                    await real_block(schedule, now, notices)
                    pending = notices
                assert pending, "the firing queued no hold notice"
                raise RuntimeError("firing rolled back after its notice was queued")

            async def notify(*args, **kwargs):
                try:
                    async with env.pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute("SET LOCAL lock_timeout = '500ms'")
                            await conn.fetchrow("SELECT id FROM schedules WHERE id = $1 FOR UPDATE", sid)
                except asyncpg.exceptions.LockNotAvailableError:
                    published.append("while the firing still held its row")
                    return
                published.append("sent")
                await real_notify(*args, **kwargs)

            monkeypatch.setattr(db_schedules, "lock_schedule", lock)
            env.scheduler._usage_block = block
            monkeypatch.setattr(usage_windows, "notify_hold", notify)
            with pytest.raises(RuntimeError, match="firing rolled back after its notice was queued"):
                await env.scheduler.run_now(sid)
            assert published == [], f"a rolled-back firing sent a hold notice: {published!r}"
            assert await env.pool.fetchval("SELECT requirement FROM schedules WHERE id = $1", sid) == original
            assert await env.pool.fetchval(
                "SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard_hold:%'") == 0
            assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 0

            usage_windows.clear_schedule(sid)
            usage_windows.clear_cache()
            env.scheduler._usage_block = real_block
            with pytest.raises(usage_windows.ScheduleDeferred):
                await env.scheduler.run_now(sid)
            stored = await env.pool.fetchval("SELECT requirement FROM schedules WHERE id = $1", sid)
            assert published == ["sent"] and stored == marker, (
                "a hold notice is sent only after its firing commits "
                f"(notice={published!r}, requirement={stored!r})")
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_subscription_run_now_under_a_rejected_window_is_refused(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import gate_service, usage_windows

        sent: list[str] = []

        async def sender(_channel, _chat, text, **_kwargs):
            sent.append(text)
            return True, None

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", AsyncMockTarget())
            await lark_targets(env.pool)
            sid = await started(env)
            before = await cursor(env, sid)
            resets = env.clock.now + timedelta(minutes=30)
            await _window(env.pool, status="rejected", utilization=1, resets_at=resets, observed_at=env.clock.now)
            status, payload = await env.call("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
            assert status == 409, payload
            assert payload["error"]["message"] == _hold_message(resets)
            assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 0
            assert await cursor(env, sid) == before
            assert len(sent) == 1
            assert await env.pool.fetchval(
                "SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard_hold:%'") == 1
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_stale_run_confirmation_sends_no_hold_notice(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import gate_service, schedules, usage_windows
        from looper.agents.projects import HTTP_409, RequestError
        from looper.db import schedules as db_schedules

        sent: list[str] = []

        async def sender(_channel, _chat, text, **_kwargs):
            sent.append(text)
            return True, None

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", AsyncMockTarget())
            await lark_targets(env.pool)
            sid = await started(env)
            before = await cursor(env, sid)
            row = await db_schedules.get_schedule(sid)
            project_dir = await env.pool.fetchval(
                "SELECT work_dir FROM projects WHERE id = $1", row["project_id"])
            confirmed = db_schedules.configuration_version(row, project_work_dir=project_dir)
            await env.pool.execute(
                "UPDATE schedules SET name = $2 WHERE id = $1", sid, f"renamed-{sid}")
            resets = env.clock.now + timedelta(minutes=30)
            await _window(env.pool, status="rejected", utilization=1, resets_at=resets, observed_at=env.clock.now)
            with pytest.raises(RequestError) as caught:
                await schedules.run_now(sid, env.scheduler, expected_version=confirmed)
            assert caught.value.status == HTTP_409
            assert str(caught.value) == (
                "the schedule configuration changed; run the tool again to ask for confirmation")
            assert sent == []
            assert await cursor(env, sid) == before
            assert await env.pool.fetchval("SELECT count(*) FROM schedule_runs WHERE schedule_id = $1", sid) == 0
            assert await env.pool.fetchval(
                "SELECT count(*) FROM system_settings WHERE key LIKE 'cost_guard_hold:%'") == 0
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_agent_run_now_still_starts_under_a_rejected_window(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import gate_service, usage_windows

        sent: list[str] = []

        async def sender(_channel, _chat, text, **_kwargs):
            sent.append(text)
            return True, None

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            usage_windows.configure(sender)
            monkeypatch.setattr(gate_service, "dashboard_target", AsyncMockTarget())
            sid = (await env.enabled())["schedule_id"]
            resets = env.clock.now + timedelta(minutes=30)
            await _window(env.pool, status="rejected", utilization=1, resets_at=resets, observed_at=env.clock.now)
            started_run = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
            assert started_run["run"]["trigger"] == "manual"
            assert await _stored_run(env, sid) == {"trigger": "manual", "status": "running", "error": None}
            assert sent == []
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


async def _watch_window_reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from looper.agents import usage_windows

    counts = {"windows": 0, "failures": 0}
    real_windows = usage_windows.read_windows
    real_failures = usage_windows.latest_quota_failure

    async def windows(*args, **kwargs):
        counts["windows"] += 1
        return await real_windows(*args, **kwargs)

    async def failures(*args, **kwargs):
        counts["failures"] += 1
        return await real_failures(*args, **kwargs)

    monkeypatch.setattr(usage_windows, "read_windows", windows)
    monkeypatch.setattr(usage_windows, "latest_quota_failure", failures)
    return counts


def test_observe_subscription_run_now_fires_like_an_agent(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import cost_guard, cost_guard_settings as settings, usage_windows

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            await settings.update({"mode": "observe"}, pool=env.pool)
            await cost_guard.refresh()
            cost_guard.current().clock = env.clock
            await lark_targets(env.pool)
            sid = await started(env)
            before = await cursor(env, sid)
            agent = (await env.enabled())["schedule_id"]
            await _window(env.pool, status="rejected", utilization=1,
                          resets_at=env.clock.now + timedelta(minutes=30), observed_at=env.clock.now)
            counts = await _watch_window_reads(monkeypatch)
            usage_windows.clear_cache()
            agent_run = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": agent})
            assert agent_run["run"]["trigger"] == "manual"
            assert await _stored_run(env, agent) == {"trigger": "manual", "status": "running", "error": None}
            agent_reads = (counts["windows"], counts["failures"])
            assert agent_reads == (1, 1)
            counts["windows"] = counts["failures"] = 0
            usage_windows.clear_cache()
            subscription = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
            assert subscription["run"]["status"] == "skipped_gate"
            assert await _stored_run(env, sid) == {
                "trigger": "manual", "status": "skipped_gate", "error": "no new messages in the window"}
            assert await cursor(env, sid) == before
            assert (counts["windows"], counts["failures"]) == agent_reads
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


def test_off_run_now_matches_the_recorded_rows_and_reads_no_window(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import cost_guard, cost_guard_settings as settings, usage_windows

        try:
            await _enforce(env.pool, clock=env.clock, day_start=env.scheduler.day_start)
            await settings.update({"mode": "off"}, pool=env.pool)
            await cost_guard.refresh()
            cost_guard.current().clock = env.clock
            await lark_targets(env.pool)
            sid = await started(env)
            before = await cursor(env, sid)
            agent = (await env.enabled())["schedule_id"]
            due = (await env.enabled())["schedule_id"]
            await env.due(due)
            await _window(env.pool, status="rejected", utilization=1,
                          resets_at=env.clock.now + timedelta(minutes=30), observed_at=env.clock.now)
            counts = await _watch_window_reads(monkeypatch)
            usage_windows.clear_cache()
            subscription = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": sid})
            assert subscription["run"]["status"] == "skipped_gate"
            assert await _stored_run(env, sid) == {
                "trigger": "manual", "status": "skipped_gate", "error": "no new messages in the window"}
            assert await cursor(env, sid) == before
            assert (counts["windows"], counts["failures"]) == (0, 0)
            usage_windows.clear_cache()
            agent_run = await env.ok("POST", "/api/v1/schedule_admin/run", {"schedule_id": agent})
            assert agent_run["run"]["trigger"] == "manual"
            assert await _stored_run(env, agent) == {"trigger": "manual", "status": "running", "error": None}
            assert (counts["windows"], counts["failures"]) == (0, 0)
            await env.pool.execute(
                "UPDATE schedules SET next_fire_at = $2 WHERE id <> $1", due, env.clock.now + timedelta(days=1))
            usage_windows.clear_cache()
            await env.scheduler.tick()
            assert env.bus.of("task_start")
            assert (await _stored_run(env, due))["status"] == "running"
            assert (counts["windows"], counts["failures"]) == (0, 0)
        finally:
            await _quiet()

    run_scheduled(test, monkeypatch, tmp_path)


_STAND_IN = r'''
import json, os, sys
turn = json.loads(sys.stdin.readline())["message"]["content"]
with open(os.environ["OUTBOUND_SEEN"], "a") as seen:
    seen.write(turn + "\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}", "session_id": "s"}))
'''
