"""One failure period of one schedule alert is one episode.

A recovery is not sent while that episode's alert is still in flight, an alert is not sent
after its recovery was claimed, and a later episode never reuses the previous episode's text.
An uncertain send is not repeated. The scheduler tick does not wait for a send. Deleting the
schedule does not raise. Completion notices are not rows of this table: a failed one is sent
again the next time that run ends, whether the channel refused it or the call raised.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from looper.agents.schedule_notify import Kind, _summary_line
from looper.db import schedules as db_schedules
from tests.db_support import DSN
from tests.schedule_support import ScheduleEnv, run_scheduled
from tests.test_schedule_notice_retry_db import Channel, _fire, _pass, _retries, _setup

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SEED = 20261128
_EP = re.compile(r"ep(\d+)")
_OUTCOME = {"OK": "sent", "REJ": "rejected", "UNK": "unknown"}


def test_a_notice_still_sending_is_described_as_maybe_not_delivered() -> None:
    moment = datetime(2026, 1, 15, 20, 4, tzinfo=timezone.utc)
    sending = _summary_line(
        {"state": "sending", "created_at": moment, "kind": "failures", "failures": 2}, "America/Los_Angeles")
    assert "可能没有送达" in sending and "涉及连续 2 次运行" in sending
    for state in ("gave_up", "superseded"):
        line = _summary_line(
            {"state": state, "created_at": moment, "kind": "failures", "failures": 2}, "America/Los_Angeles")
        assert "没有送达" in line and "可能没有送达" not in line


class Recording(Channel):
    def __init__(self) -> None:
        super().__init__("sent")
        self.log: list[dict[str, Any]] = []

    async def __call__(self, channel: str, chat: str, text: str, *, proactive: Any = None,
                       group: bool = False) -> tuple[bool, str | None]:
        step = self.script[0] if self.script else self.default
        self.log.append({"text": text, "outcome": step, "ep": _episode(text), "role": _role(text)})
        return await super().__call__(channel, chat, text, proactive=proactive, group=group)


def _episode(text: str) -> int | None:
    match = _EP.search(text)
    return int(match.group(1)) if match else None


def _role(text: str) -> str | None:
    if text.startswith("告警"):
        return "alert"
    if text.startswith("恢复"):
        return "recovery"
    return None


class _World:
    def __init__(self, env: ScheduleEnv, channel: Recording, sid: str, family: str) -> None:
        self.env = env
        self.channel = channel
        self.sid = sid
        self.family = family
        self.deleted = False
        self.episode = 0
        self.counts: dict[int, int] = {}
        self.open_at: dict[int, int] = {}
        self.sched: Any = None
        self.alert_kind = Kind.FAILURES_ALERT if family == "failures" else Kind.STALE_ALERT
        self.recovery_kind = Kind.FAILURES_RECOVERED if family == "failures" else Kind.STALE_RECOVERED

    async def refresh(self) -> None:
        if not self.deleted:
            self.sched = await db_schedules.get_schedule(self.sid)

    async def flush(self) -> None:
        await self.env.scheduler._notifier.retry_due(self.env.clock.now)

    async def apply(self, event: str, count: int) -> None:
        if self.deleted and event in ("T", "REC", "DUE", "RST"):
            before = len(self.channel.log)
            try:
                if event == "T":
                    await self._open_alert(count)
                elif event == "REC":
                    await self._recover()
                elif event == "DUE":
                    await self._due()
                else:
                    await self.env.scheduler._notifier.settle_stopped()
            except Exception as exc:
                raise AssertionError(f"I8 raised after delete on {event}: {type(exc).__name__}: {exc}") from exc
            assert len(self.channel.log) == before, "I8 sent after the schedule was deleted"
            return
        if event in _OUTCOME:
            self.channel.default = _OUTCOME[event]
            return
        if event == "T":
            await self._open_alert(count)
        elif event == "REC":
            await self._recover()
        elif event == "DUE":
            await self._due()
        elif event == "RST":
            await self.env.scheduler._notifier.settle_stopped()
        elif event == "DEL":
            # A repeated delete must not drop the row captured before the first one. Later events still
            # hand that row to delivery, which finds the schedule gone and logs it.
            if not self.deleted:
                self.sched = await db_schedules.get_schedule(self.sid)
                await self.env.pool.execute("DELETE FROM schedules WHERE id = $1", self.sid)
                self.deleted = True
        else:
            raise AssertionError(event)

    async def _open_alert(self, count: int) -> None:
        self.episode += 1
        episode = self.episode
        self.counts[episode] = count
        self.open_at[episode] = len(self.channel.log)
        if not self.deleted:
            await self.env.pool.execute(
                "UPDATE schedules SET consecutive_failures = $2 WHERE id = $1", self.sid, count)
        await self.refresh()
        before = len(self.channel.log)
        await self.env.scheduler._notifier._deliver(self.sched, self.alert_kind, f"告警：ep{episode}")
        await self.flush()
        if self.deleted:
            return
        fresh = [item for item in self.channel.log[before:] if item["role"] == "alert"]
        assert fresh, f"I4 episode {episode} was not sent ({self.family})"
        assert all(item["ep"] == episode for item in fresh), fresh

    async def _recover(self) -> None:
        episode = self.episode
        if not self.deleted:
            await self.refresh()
        before = len(self.channel.log)
        await self.env.scheduler._notifier._deliver(self.sched, self.recovery_kind, f"恢复：ep{episode}")
        await self.flush()
        for index in range(before, len(self.channel.log)):
            item = self.channel.log[index]
            if item["role"] != "recovery":
                continue
            self._named(item, index)

    def _named(self, item: dict[str, Any], index: int) -> None:
        prior = [row for at, row in enumerate(self.channel.log)
                 if at < index and row["ep"] == item["ep"] and row["role"] == "alert"]
        text = item["text"]
        last = prior[-1]["outcome"] if prior else "sent"
        if last == "sent":
            assert "补充" not in text, text
            return
        if last in ("unknown", "raise"):
            assert "可能没有送达" in text, text
        else:
            assert "没有送达" in text and "可能没有送达" not in text, text
            if self.family == "failures":
                assert "失败告警" in text and f"涉及连续 {self.counts[item['ep']]} 次运行" in text, text
        assert text.count("补充") == 1, text

    async def _due(self) -> None:
        self.env.clock.now += timedelta(minutes=20)
        if not self.deleted:
            await self.env.pool.execute(
                "UPDATE schedules SET next_fire_at = $2 WHERE id = $1", self.sid,
                self.env.clock.now + timedelta(days=30))
        await self.flush()

    def check(self) -> None:
        log = self.channel.log
        for episode in self.open_at:
            alerts = [(index, row) for index, row in enumerate(log)
                      if row["ep"] == episode and row["role"] == "alert"]
            recoveries = [index for index, row in enumerate(log)
                          if row["ep"] == episode and row["role"] == "recovery"]
            if recoveries:
                assert all(index < recoveries[0] for index, _row in alerts), ("I1", episode, log)
            delivered = [index for index, row in alerts if row["outcome"] == "sent"]
            assert len(delivered) <= 1, ("I2", episode, log)
            stopped = [index for index, row in alerts if row["outcome"] in ("sent", "unknown", "raise")]
            if stopped:
                assert all(index <= min(stopped) for index, _row in alerts), ("I2", episode, log)
            for role in ("alert", "recovery"):
                rows = [(index, row) for index, row in enumerate(log)
                        if row["ep"] == episode and row["role"] == role]
                unknown = [index for index, row in rows if row["outcome"] in ("unknown", "raise")]
                if unknown:
                    assert all(index <= unknown[0] for index, _row in rows), ("I3", episode, role, log)
        for episode, opened in self.open_at.items():
            if episode - 1 not in self.open_at:
                continue
            later = [row for index, row in enumerate(log)
                     if index >= opened and row["role"] == "recovery" and row["ep"] == episode - 1]
            assert not later, ("I6", episode - 1, later)


async def _ticks_while_a_send_hangs(env: ScheduleEnv, sid: str, channel: Recording,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    channel.script = ["rejected"]
    schedule = await db_schedules.get_schedule(sid)
    await env.scheduler._notifier._deliver(schedule, Kind.STALE_ALERT, "告警：hang")
    await env.scheduler._notifier.retry_due(env.clock.now)
    env.clock.now += timedelta(minutes=2)
    started, release = asyncio.Event(), asyncio.Event()

    async def hang(*_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        started.set()
        await release.wait()
        return True, None

    ticks: list[int] = []

    async def tick() -> None:
        ticks.append(1)
        await asyncio.sleep(0)

    env.scheduler._notifier._sender = hang
    env.scheduler._tick_seconds = 0.01
    real_tick = env.scheduler.tick
    monkeypatch.setattr(env.scheduler, "tick", tick)
    loop = asyncio.create_task(env.scheduler._loop())
    try:
        await asyncio.wait_for(started.wait(), 2)

        async def enough() -> None:
            while len(ticks) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(enough(), 2)
    finally:
        loop.cancel()
        release.set()
        await asyncio.gather(loop, return_exceptions=True)
        pending = env.scheduler._notice_retries
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        env.scheduler.tick = real_tick
        env.scheduler._notifier._sender = channel
        channel.script.clear()
    assert len(ticks) > 1, "I7"


def test_episode_sequences_keep_alert_and_recovery_sends_apart(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    """Seeded sequences over trigger, outcome, due, recovery, delete and restart. The seed is fixed."""

    async def test(env: ScheduleEnv) -> None:
        print(f"schedule notice episode seed {SEED}", flush=True)
        rng = random.Random(SEED)
        sid, _created = await _setup(env)
        channel = Recording()
        env.scheduler._notifier._sender = channel
        await _ticks_while_a_send_hangs(env, sid, channel, monkeypatch)
        print("I7 ok", flush=True)
        channel.log.clear()
        channel.attempts.clear()
        channel.got.clear()
        fixed = [
            ("stale", ["REJ", "T", "REC", "DUE"]),
            ("failures", ["REJ", "T", "T", "DUE"]),
            ("stale", ["REJ", "T", "REJ", "REC", "T", "DUE"]),
            ("failures", ["UNK", "T", "DUE"]),
            ("failures", ["OK", "T", "REC"]),
        ]
        random_ones = [
            (rng.choice(["failures", "stale"]), [rng.choice(["T", "OK", "REJ", "UNK", "DUE", "REC", "DEL", "RST"])
                                                 for _ in range(rng.randint(1, 30))])
            for _ in range(500)
        ]
        for number, (family, events) in enumerate([*fixed, *random_ones]):
            world_sid = sid
            if not await db_schedules.get_schedule(world_sid):
                world_sid, _again = await _setup(env)
                env.scheduler._notifier._sender = channel
                sid = world_sid
            await env.pool.execute("DELETE FROM schedule_notice_retries WHERE schedule_id = $1", sid)
            await env.pool.execute(
                "UPDATE schedules SET failures_alert_at = NULL, stale_alert_at = NULL, "
                "consecutive_failures = 0 WHERE id = $1", sid)
            channel.log.clear()
            channel.attempts.clear()
            channel.got.clear()
            channel.script.clear()
            channel.default = "sent"
            world = _World(env, channel, sid, family)
            try:
                for event in events:
                    await world.apply(event, rng.randint(1, 4))
                world.check()
            except AssertionError as exc:
                raise AssertionError(
                    f"seed {SEED} sequence {number} family {family} events {events}: {exc}") from exc
            if world.deleted:
                sid = ""
        print(f"schedule notice episode seed {SEED} sequences {len(fixed) + len(random_ones)}", flush=True)

    run_scheduled(test, monkeypatch, tmp_path)


def test_refused_stale_alert_is_not_sent_after_its_recovery(monkeypatch: pytest.MonkeyPatch,
                                                           tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, gate_command="true", alert_stale_after_seconds=300)
        await _fire(env, sid)
        channel.script = ["rejected", "rejected", "rejected"]
        await _pass(env, sid, 60)
        await _pass(env, sid, 1)
        await _pass(env, sid, 5)
        [row] = await _retries(env, "stale_alert")
        assert (row["state"], row["retries"], channel.got[1:]) == ("pending", 2, [])
        original = next(text for text in channel.attempts if text.startswith("告警"))
        await env.due(sid)
        await env.scheduler.tick()
        await env.scheduler.idle()
        [row] = await _retries(env, "stale_alert")
        assert row["state"] == "superseded" and row["episode"] == 1
        assert [text for text in channel.got if text.startswith("恢复")], channel.got
        for minutes in (15, 30):
            await _pass(env, sid, minutes)
        recovered_at = next(index for index, text in enumerate(channel.attempts) if text.startswith("恢复"))
        # A later stale period may alert again. It must not send this episode's text.
        assert all(text != original for text in channel.attempts[recovered_at + 1:])

    run_scheduled(test, monkeypatch, tmp_path)


def test_a_second_stale_alert_is_not_swallowed_by_the_old_pending_one(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, gate_command="true", alert_stale_after_seconds=300)
        await _fire(env, sid)
        channel.script = ["rejected", "rejected", "rejected"]
        for minutes in (60, 1, 5):
            await _pass(env, sid, minutes)
        await env.due(sid)
        await env.scheduler.tick()
        await env.scheduler.idle()
        before = [text for text in channel.attempts if text.startswith("告警")]
        await _pass(env, sid, 60)
        new = [text for text in channel.attempts if text.startswith("告警")][len(before):]
        opened = await env.pool.fetchval("SELECT stale_alert_at FROM schedules WHERE id = $1", sid)
        assert opened is not None and any(text != before[0] for text in new)

    run_scheduled(test, monkeypatch, tmp_path)


@pytest.mark.parametrize("how", ["raise", "refused"])
def test_a_failed_completion_notice_is_replayed_when_the_run_ends_again(
    how: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A completion notice is not a retry row. A refused send and a raised send are both sent again later."""

    async def test(env: ScheduleEnv) -> None:
        sid, channel = await _setup(env, "raise" if how == "raise" else "rejected", notify="failures",
                                    alert_failures_after=10)
        run = await _fire(env, sid)
        assert await env.pool.fetchval(
            "SELECT count(*) FROM schedule_notice_retries WHERE kind = 'run'") == 0
        before = len(channel.attempts)
        await env.scheduler._notifier.run_ended(run["run_id"])
        assert len(channel.attempts) == before + 1
        for minutes in (1, 5, 15, 30):
            await _pass(env, sid, minutes)
        assert len(channel.attempts) == before + 1
        await env.scheduler._notifier.run_ended(run["run_id"])
        assert len(channel.attempts) == before + 2
        assert await env.pool.fetchval("SELECT id FROM schedules WHERE id = $1", sid) == sid

    run_scheduled(test, monkeypatch, tmp_path)
