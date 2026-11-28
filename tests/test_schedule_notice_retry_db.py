"""A schedule notice whose send failed: kept in ``schedule_notice_retries``, tried again a bounded number of times
when the channel says it did not send it, never again when it may have, and named by the recovery that follows an
alert the chat did not get.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database; it uses ``schedule_support``. The channels
are a stand-in: nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.db_support import DSN
from tests.schedule_support import TELEGRAM_CHAT, ScheduleEnv, Sender, allow_telegram, run_scheduled, with_sender

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

TARGET = {"channel": "telegram", "chat": TELEGRAM_CHAT}


class Channel(Sender):
    """Answers each send with the next of ``script``: "sent", "rejected" (nothing went), "unknown" (it may have
    gone), "raise" (it may have gone: the channel accepted it and then the call failed) or "cancel" (the server stops
    in the middle of the send). ``default`` once the script is used up. ``got`` is what reached the chat."""

    def __init__(self, default: str = "sent") -> None:
        super().__init__()
        self.default = default
        self.script: list[str] = []
        self.attempts: list[str] = []
        self.got: list[str] = []

    async def __call__(self, channel: str, chat: str, text: str, *, proactive: Any = None,
                       group: bool = False) -> tuple[bool, str | None]:
        from looper.agents.channels.base import NotifyAnswer, PostOutcome

        self.attempts.append(text)
        step = self.script.pop(0) if self.script else self.default
        if step == "sent":
            self.got.append(text)
            return NotifyAnswer(PostOutcome.SENT)
        if step == "rejected":
            return NotifyAnswer(PostOutcome.REJECTED, "the channel is not running")
        if step == "unknown":
            self.got.append(text)
            return NotifyAnswer(PostOutcome.UNKNOWN, "the channel did not answer in time")
        self.got.append(text)
        if step == "cancel":
            raise asyncio.CancelledError
        raise TimeoutError("the answer was lost")

    def alerts(self) -> list[str]:
        return [text for text in self.attempts if text.startswith("告警")]


def _channel(env: ScheduleEnv, channel: Channel) -> Channel:
    env.scheduler._notifier._sender = channel
    return channel


async def _setup(env: ScheduleEnv, default: str = "sent", **fields: Any) -> tuple[str, Channel]:
    with_sender(env)
    channel = _channel(env, Channel(default))
    await allow_telegram(env)
    # The column's maximum. A retry test that waits hours must not also emit the default 15-minute stale alert.
    # 60 is below the column minimum (300); the stale test sets 300 itself.
    body = {"gate_command": "exit 3", "notify": "alerts", "notify_target": TARGET, "alert_failures_after": 1,
            "alert_stale_after_seconds": 2_592_000, **fields}
    return (await env.enabled(**body))["schedule_id"], channel


async def _fire(env: ScheduleEnv, sid: str) -> dict[str, Any]:
    await env.due(sid)
    await env.scheduler.tick()
    await env.scheduler.idle()
    return (await env.runs(sid))[0]


async def _pass(env: ScheduleEnv, sid: str, minutes: float) -> None:
    """A scheduler pass ``minutes`` later in which the schedule itself does not fire."""
    env.clock.now += timedelta(minutes=minutes)
    await env.pool.execute("UPDATE schedules SET next_fire_at = $2 WHERE id = $1", sid,
                           env.clock.now + timedelta(days=30))
    await env.scheduler.tick()
    await env.scheduler.idle()


async def _retries(env: ScheduleEnv, kind: str = "failures_alert") -> list[dict[str, Any]]:
    return [dict(row) for row in await env.pool.fetch(
        "SELECT * FROM schedule_notice_retries WHERE kind = $1 ORDER BY id", kind)]


async def _outbox(env: ScheduleEnv, sid: str, kind: str) -> list[str]:
    rows = await env.pool.fetch("SELECT status FROM schedule_notifications WHERE schedule_id = $1 AND kind = $2 "
                                "ORDER BY created_at, id", sid, kind)
    return [row["status"] for row in rows]


async def _recover(env: ScheduleEnv, sid: str) -> None:
    await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"gate_command": "true"}})
    await _fire(env, sid)


def test_a_rejected_alert_is_retried_after_its_backoff_until_it_is_sent(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        run = await _fire(env, sid)
        [row] = await _retries(env)
        assert (row["state"], row["retries"], row["run_id"], row["failures"]) == ("pending", 0, run["run_id"], 1)

        await _pass(env, sid, 0.5)  # not due yet
        assert len(channel.alerts()) == 1
        await _pass(env, sid, 1)
        [row] = await _retries(env)
        assert (row["state"], row["retries"]) == ("sent", 1)
        assert channel.got == [channel.alerts()[0]]
        assert await _outbox(env, sid, "failures_alert") == ["failed", "sent"]
        await _pass(env, sid, 120)
        assert len(channel.alerts()) == 2

    run_scheduled(test, monkeypatch, tmp_path)


def test_three_failed_retries_give_up_and_the_recovery_says_what_the_chat_did_not_get(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, "rejected")
        await _fire(env, sid)
        for minutes, attempts in ((1, 2), (4, 2), (1, 3), (14, 3), (1, 4)):
            await _pass(env, sid, minutes)
            assert len(channel.alerts()) == attempts, minutes
        [row] = await _retries(env)
        assert (row["state"], row["retries"], row["next_attempt_at"]) == ("gave_up", 3, None)
        await _pass(env, sid, 600)
        assert len(channel.alerts()) == 4

        channel.default = "sent"
        await _recover(env, sid)
        [recovery] = channel.got
        assert recovery.startswith("恢复")
        [line] = [line for line in recovery.splitlines() if line.startswith("补充")]
        assert "没有送达" in line and "涉及连续 1 次运行" in line and "2026-01-15 12:" in line
        [row] = await _retries(env)
        assert row["state"] == "gave_up" and row["reported_at"] is not None

        # Told once: the next streak's recovery does not repeat it.
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"gate_command": "exit 3"}})
        await _fire(env, sid)
        await _recover(env, sid)
        assert "补充" not in channel.got[-1] and channel.got[-1].startswith("恢复")

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_pending_alert_is_not_sent_after_its_recovery_which_carries_it(monkeypatch: pytest.MonkeyPatch,
                                                                         tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        await _recover(env, sid)
        assert [text.startswith("恢复") and "没有送达" in text for text in channel.got] == [True]
        [row] = await _retries(env)
        assert row["state"] == "gave_up"
        await _pass(env, sid, 120)
        assert len(channel.alerts()) == 1

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_recovery_that_fails_is_retried_with_the_undelivered_alert(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected", "rejected"]
        await _fire(env, sid)
        await _recover(env, sid)
        [alert] = await _retries(env)
        assert alert["reported_at"] is None
        await _pass(env, sid, 2)
        assert [text.startswith("恢复") and "没有送达" in text for text in channel.got] == [True]
        assert channel.got[0].count("补充") == 1
        [alert] = await _retries(env)
        assert alert["reported_at"] is not None

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_new_alert_drops_the_pending_recovery_and_its_recovery_still_names_the_old_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected", "rejected"]
        await _fire(env, sid)
        await _recover(env, sid)
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"gate_command": "exit 3"}})
        await _fire(env, sid)
        [recovery] = await _retries(env, "failures_recovered")
        assert recovery["state"] == "gave_up"
        await _pass(env, sid, 120)
        assert [text[:2] for text in channel.got] == ["告警"]
        await _recover(env, sid)
        assert "没有送达" in channel.got[-1]

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("answer", ["unknown", "raise"])
def test_an_alert_that_may_have_gone_is_uncertain_and_never_sent_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: str,
) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = [answer]
        await _fire(env, sid)
        [row] = await _retries(env)
        assert (row["state"], row["next_attempt_at"]) == ("uncertain", None)
        await _pass(env, sid, 600)
        assert len(channel.alerts()) == 1
        await _recover(env, sid)
        assert "可能没有送达" in channel.got[-1]

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_retry_that_may_have_gone_is_not_sent_again(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected", "raise"]
        await _fire(env, sid)
        await _pass(env, sid, 2)
        await _pass(env, sid, 600)
        [row] = await _retries(env)
        assert (row["state"], row["retries"], len(channel.alerts())) == ("uncertain", 1, 2)

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_stop_in_the_middle_of_a_retry_does_not_send_it_twice(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    from looper.agents.scheduler import Scheduler

    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected", "cancel"]
        await _fire(env, sid)
        await _pass(env, sid, 2)
        [row] = await _retries(env)
        assert (row["state"], row["retries"]) == ("sending", 1)  # claimed before the send

        await env.scheduler.stop()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=channel, runtime_root=env.runtime_root,
                                  tick_seconds=3600)
        await env.scheduler.start()
        await _pass(env, sid, 600)
        [row] = await _retries(env)
        assert row["state"] == "uncertain" and "stopped" in row["error"]
        assert len(channel.alerts()) == 2

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_pending_retry_is_sent_once_by_the_server_that_starts_next(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    from looper.agents.scheduler import Scheduler

    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        await env.scheduler.stop()
        env.scheduler = Scheduler(bus=env.bus, clock=env.clock, sender=channel, runtime_root=env.runtime_root,
                                  tick_seconds=3600)
        await env.scheduler.start()
        await _pass(env, sid, 2)
        env.clock.now += timedelta(hours=1)
        await asyncio.gather(env.scheduler._notifier.retry_due(env.clock.now),
                             env.scheduler._notifier.retry_due(env.clock.now))
        assert (len(channel.alerts()), len(channel.got)) == (2, 1)

    run_scheduled(test, monkeypatch, tmp_path)


def test_two_passes_at_once_claim_a_retry_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Two notifiers (no lock in common): only the claim keeps the send single."""
    from looper.agents.schedule_notify import Notifier

    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        env.clock.now += timedelta(minutes=2)
        await asyncio.gather(*[Notifier(sender=channel, clock=env.clock).retry_due(env.clock.now) for _ in range(3)])
        assert (len(channel.alerts()), len(channel.got)) == (2, 1)

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_refused_run_notice_is_replayed_and_a_second_run_is_its_own(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    """A completion replay of a run whose send was refused sends again. One row stays. A second run is its own."""
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, "rejected", notify="failures", alert_failures_after=10)
        run = await _fire(env, sid)
        await env.scheduler._notifier.run_ended(run["run_id"])
        [row] = await _retries(env, "run")
        assert (row["state"], row["run_id"], len(channel.attempts)) == ("pending", run["run_id"], 2)
        # A second failed run is a notice of its own.
        await _fire(env, sid)
        assert len(await _retries(env, "run")) == 2
        channel.default = "sent"
        # due() moves the clock about five minutes, so the first notice is already retried once and waits five more.
        await _pass(env, sid, 5)
        assert [row["state"] for row in await _retries(env, "run")] == ["sent", "sent"]
        assert len(channel.got) == 2

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_stale_alert_without_a_run_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, gate_command="true", alert_stale_after_seconds=300)
        await _fire(env, sid)
        channel.script = ["rejected"]
        await _pass(env, sid, 60)
        [row] = await _retries(env, "stale_alert")
        assert (row["state"], row["run_id"], row["failures"]) == ("pending", None, None)
        await _pass(env, sid, 2)
        assert [row["state"] for row in await _retries(env, "stale_alert")] == ["sent"]
        assert len(channel.got) == 1

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("change", [{"notify": "never"}, {"notify_target": None}])
def test_a_retry_is_given_up_when_the_schedule_no_longer_sends_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, Any],
) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": change})
        await _pass(env, sid, 2)
        [row] = await _retries(env)
        assert (row["state"], len(channel.alerts())) == ("gave_up", 1)

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_retry_stays_within_the_channels_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        await env.pool.execute("UPDATE channel_configs SET config = config || '{\"allowed_user_ids\": []}'::jsonb")
        await _pass(env, sid, 2)
        [row] = await _retries(env)
        assert (row["state"], len(channel.alerts())) == ("gave_up", 1)
        assert "allowlist" in row["error"]

    run_scheduled(test, monkeypatch, tmp_path)


def test_no_retry_is_sent_while_the_server_drains(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.agents.drain import DrainState

    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        drain = DrainState()
        env.scheduler._drain = drain
        monkeypatch.setattr(type(drain), "draining", property(lambda self: True))
        await _pass(env, sid, 2)
        assert len(channel.alerts()) == 1
        monkeypatch.setattr(type(drain), "draining", property(lambda self: False))
        await _pass(env, sid, 1)
        assert len(channel.got) == 1

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_delivered_notice_and_a_dashboard_one_leave_no_retry(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        await _fire(env, sid)
        other = (await env.enabled(gate_command="exit 3", alert_failures_after=1))["schedule_id"]
        await _fire(env, other)
        assert await env.pool.fetchval("SELECT count(*) FROM schedule_notice_retries") == 0
        await _recover(env, sid)
        assert "补充" not in channel.got[-1]

    run_scheduled(test, monkeypatch, tmp_path)


def test_old_settled_retries_are_pruned_with_the_outbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from looper.db import schedules as db_schedules

    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, "rejected", notify="failures", alert_failures_after=10)
        await _fire(env, sid)
        await _fire(env, sid)
        first, second = await _retries(env, "run")
        await env.pool.execute("UPDATE schedule_notice_retries SET state = 'gave_up' WHERE id = $1", first["id"])
        async with env.pool.acquire() as conn:
            await db_schedules.prune_notifications(conn, env.clock.now + timedelta(days=1))
        assert [row["id"] for row in await _retries(env, "run")] == [second["id"]]  # a pending one is kept

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_sent_retry_is_not_named_by_the_recovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A later successful try stays ``sent``. The recovery does not call that alert undelivered."""
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        await _pass(env, sid, 2)
        [row] = await _retries(env)
        assert row["state"] == "sent" and row["retries"] == 1
        await _recover(env, sid)
        assert "补充" not in channel.got[-1]

    run_scheduled(test, monkeypatch, tmp_path)


def test_the_summary_counts_failures_at_the_latest_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, "rejected")
        await _fire(env, sid)
        await _fire(env, sid)
        [row] = await _retries(env)
        assert row["failures"] == 2 and row["state"] == "pending"
        channel.default = "sent"
        await _recover(env, sid)
        assert "涉及连续 2 次运行" in channel.got[-1]

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_successful_run_retry_marks_the_completion_event_sent(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.db import schedules as db_schedules

        with_sender(env)
        channel = _channel(env, Channel("rejected"))
        await allow_telegram(env)
        schedule = await env.schedule(notify="always", notify_target=TARGET)
        run_id = await env.scheduler.run_now(schedule["schedule_id"])
        task_id = (await db_schedules.get_run(run_id))["task_id"]
        await env.end_task(task_id, "completed", summary="done")
        await env.scheduler.idle()
        event = await env.pool.fetchrow(
            "SELECT sent_at FROM manager_events WHERE dedupe_key = $1", f"schedule_run:{run_id}")
        assert event is not None and event["sent_at"] is None
        [row] = await _retries(env, "run")
        assert row["state"] == "pending"
        channel.default = "sent"
        # The schedule is not enabled, so its next fire stays unset. Advance the clock and retry.
        env.clock.now += timedelta(minutes=2)
        await env.scheduler.idle()
        sent_at = await env.pool.fetchval(
            "SELECT sent_at FROM manager_events WHERE dedupe_key = $1", f"schedule_run:{run_id}")
        assert sent_at is not None
        delivered = len(channel.got)
        await env.scheduler._notifier.run_ended(run_id)
        assert len(channel.got) == delivered

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("answer", ["pending-in-outbox", "unknown"])
def test_telegram_outbox_unknown_is_not_sent_again(answer: str, monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents import chat_settings
        from looper.agents.channels.base import PostOutcome, PostReceipt
        from looper.agents.channels.registry import ChannelRegistry
        from looper.agents.channels.telegram import TelegramChannelAdapter
        from looper.agents.message import builtin_channel_id

        monkeypatch.setattr(chat_settings, "_outbound_enabled", True)
        sends: list[str] = []

        class Outbox:
            async def send(self, kind, chat, body, reply_to, key, source, **kw):
                sends.append(key)
                error = "outbound delivery is still pending" if answer == "pending-in-outbox" else "lost answer"
                return PostReceipt(False, outcome=PostOutcome.UNKNOWN, error=error)

        adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
        adapter._app, adapter._bus, adapter._channel_outbox = object(), None, Outbox()
        adapter._channel_id = builtin_channel_id("telegram")
        registry = ChannelRegistry.__new__(ChannelRegistry)
        registry._channels = {builtin_channel_id("telegram"): adapter}
        registry.record_delivery = lambda *a: None
        sid, _ = await _setup(env)
        env.scheduler._notifier._sender = registry.notify_outcome
        await _fire(env, sid)
        for minutes in (1, 5, 15, 30):
            await _pass(env, sid, minutes)
        [row] = await _retries(env)
        assert (len(sends), row["state"]) == (1, "uncertain")

    run_scheduled(test, monkeypatch, tmp_path)


def test_alert_in_flight_when_the_recovery_is_written(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        entered, release = asyncio.Event(), asyncio.Event()
        plain = env.scheduler._notifier._sender

        async def slow(chan: str, chat: str, text: str) -> Any:
            if text.startswith("告警") and not release.is_set():
                entered.set()
                await release.wait()
                channel.script = ["rejected"]
            return await plain(chan, chat, text)

        env.scheduler._notifier._sender = slow
        env.clock.now += timedelta(minutes=1)
        retry = asyncio.create_task(env.scheduler._notifier.retry_due(env.clock.now))
        await asyncio.wait_for(entered.wait(), 10)
        await env.ok("POST", "/api/v1/schedule_admin/update", {"schedule_id": sid, "fields": {"gate_command": "true"}})
        await env.due(sid)
        await env.scheduler.tick()
        while env.scheduler._background:
            await asyncio.gather(*list(env.scheduler._background))
        release.set()
        await retry
        for minutes in (5, 15, 30):
            await _pass(env, sid, minutes)
        assert channel.got and ("没有送达" in channel.got[0]) and not any(text.startswith("告警") for text in channel.got)

    run_scheduled(test, monkeypatch, tmp_path)


def test_schedule_deleted_before_the_claim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        from looper.agents.schedule_notify import Kind
        from looper.db import schedules as db_schedules

        sid, _ = await _setup(env)
        schedule = await db_schedules.get_schedule(sid)
        await env.pool.execute("DELETE FROM schedules WHERE id = $1", sid)
        await env.scheduler._notifier._deliver(schedule, Kind.STALE_ALERT, "告警：x")

    run_scheduled(test, monkeypatch, tmp_path)


def test_retries_do_not_hold_the_scheduler_loop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env)
        channel.script = ["rejected"]
        await _fire(env, sid)
        env.clock.now += timedelta(minutes=2)
        ticks: list[int] = []
        gate = asyncio.Event()

        async def hang(*_args: Any, **_kwargs: Any) -> Any:
            await gate.wait()
            return True, None

        async def tick() -> None:
            ticks.append(1)

        env.scheduler._notifier._sender = hang
        env.scheduler._tick_seconds = 0.01
        monkeypatch.setattr(env.scheduler, "tick", tick)
        loop = asyncio.create_task(env.scheduler._loop())
        await asyncio.sleep(1)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        assert len(ticks) > 1

    run_scheduled(test, monkeypatch, tmp_path)
