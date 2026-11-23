"""The weekday morning brief (morning brief RFC): one message to the operator's direct chat.

On a workday, at ``morning_brief.time`` (09:15 by default) in ``user.timezone`` (else the server's zone), the
service sends one brief through ``deliver``: what waits for the operator (the needs-you summary), reminders due
that local day, and proactive messages held for the digest. Every brief reads from the first attempt's digest
dates (Monday also Saturday and Sunday) through the day being sent, except digest rows already recorded by id.
The claim stores the ids that read returned, in that same write. A sent or uncertain brief records those ids,
in the same transaction as its row. A refusal records none and clears the stored ids, so a Tuesday brief that
was rejected is still in Wednesday's. If the sender stops before that record, recovery consumes exactly the
stored ids and does not scan by time: a row whose insert had not committed is still read once, on the next
brief. With triage enabled, the operator DM also gets owned digest and important fallback events, using
the same window and exact-id claim/settlement/recovery policy. No model sees those rendered fields.
Nothing in any section means
nothing is sent. The switch, the channel and the operator are read again after the summary and again
immediately before the post. The switch is read once more after the operator query returns: it can be turned
off while that query runs. A fresh claim that then fails the check is deleted. A claim that continued a refusal
is put back as that refusal, with the attempt count it had, so a later day still sees the first attempt. A
brief with something to say is sent uncounted and never deferred: it is the digest. ``decide`` records that
only while ``delivery.enabled`` is on, under the same key as the post, and the channel receipt is written on
that row. A later try replaces a rejected receipt on that row. One already recorded as sent or unknown keeps
its outcome, sent time and external id.

``morning_brief.enabled`` is off by default. While it is off a tick reads the in-memory switch and stops. One
row of ``morning_briefs`` per local date and direct chat makes a restart and a second server send it at most
once; a send that stopped in the middle is ``uncertain`` and is not sent again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from ..db import connection, morning_briefs as store, proactive_deliveries as deliveries, triage_events
from ..db.triage_events import BriefEvent
from . import attention, chat_settings, proactive_delivery, triage_settings, turn_metrics
from .channels.base import PostOutcome
from .operator_identity import OPERATOR_KEY, operator_dm_target
from .proactive_delivery import ChatKind, Delivery, DeliveryKind, DeliveryState, Outcome, Urgency
from .reminders import target

logger = logging.getLogger(__name__)

TICK_SECONDS = 30.0
ATTEMPTS = 3  # refusals before the brief is left failed
STOP_GRACE_SECONDS = 15.0
_CANCEL_SHARE = 0.25
_LOCK = (741926, 1)  # session advisory lock; 741923 is intent push, 741925 is reminders
_WEEKDAYS = "一二三四五六日"
_BOT_TYPES = frozenset({"telegram", "lark", "lark_bot"})
_KIND_LABELS = {"gate_reminder": "关卡提醒", "task_notice": "任务通知", "subscription": "订阅推送",
                "intent": "意图", "completion": "收尾", "user_reminder": "提醒"}
_REASON_LABELS = {"outside_active_hours": "不在生效时段", "quiet_hours": "免打扰", "daily_cap": "超过当天上限"}
_DETAIL_MAX = 500


def digest_dates(day: date) -> tuple[date, ...]:
    """The digest dates a chat's first brief covers: Monday also takes Saturday and Sunday, so a Friday night's
    deferral is not dropped. A later brief reads what that chat has not yet consumed."""
    if day.weekday() == 0:
        return (day - timedelta(days=2), day - timedelta(days=1), day)
    return (day,)


def digest_dates_through(origin: date, day: date) -> tuple[date, ...]:
    """Digest dates from a chat's first attempt ``origin`` through ``day``.

    The attempt's own window is ``digest_dates(origin)`` (Monday also takes the weekend). While no brief has
    recorded a consumption boundary, a later day keeps reading from the start of that window."""
    start = min(digest_dates(origin))
    if day < start:
        return digest_dates(origin)
    return tuple(start + timedelta(days=offset) for offset in range((day - start).days + 1))


def is_due(local: datetime, send_at: time) -> bool:
    """Whether ``local`` (in the operator's zone) is a workday at or after ``send_at``. A minute the clocks skip
    is due on the first tick after the gap; a repeated hour is still the same local date, so the row sends once."""
    if local.weekday() >= 5:
        return False
    return local.time().replace(tzinfo=None) >= send_at.replace(tzinfo=None)


def _plain(text: str) -> str:
    return attention._plain(text)


def _reminder_line(row: Mapping[str, Any], tz: ZoneInfo) -> str:
    local = row["due_at"].astimezone(tz)
    return f"- {local:%H:%M} {_plain(row['text'])}"


def _digest_line(row: Mapping[str, Any], tz: ZoneInfo) -> str:
    kind = _KIND_LABELS.get(row["kind"], row["kind"])
    reason = _REASON_LABELS.get(row["reason"], row["reason"])
    when = row["decided_at"].astimezone(tz)
    bits = [reason, f"{when:%m-%d %H:%M}"]
    if row.get("task_id"):
        bits.append(f"任务 {row['task_id']}")
    if row.get("schedule_id"):
        bits.append(f"定时 {row['schedule_id']}")
    return f"- {kind}（{'，'.join(bits)}）"


def render(*, day: date, needs: str | None, reminders: list[Mapping[str, Any]], digests: list[Mapping[str, Any]],
           tz: ZoneInfo, triage: list[BriefEvent] = (), triage_more: int = 0) -> str | None:
    """The brief, or None when every section is empty. ``needs`` is the needs-you summary; the empty summary is
    not a section."""
    sections: list[str] = []
    if needs and needs != attention.NOTHING:
        sections.append(needs)
    if reminders:
        sections.append("今天的提醒\n" + "\n".join(_reminder_line(row, tz) for row in reminders))
    if digests:
        sections.append("推迟到这份晨报的消息\n" + "\n".join(_digest_line(row, tz) for row in digests))
    if triage:
        lines = [_triage_line(event, tz) for event in triage]
        if triage_more > 0:
            lines.append(f"另有 {triage_more} 条，见消息流页")
        sections.append("进摘要的事件\n" + "\n".join(lines))
    if not sections:
        return None
    title = f"晨报 · {day:%m-%d}（周{_WEEKDAYS[day.weekday()]}）"
    return "\n\n".join([title, *sections])


def _triage_line(event: BriefEvent, tz: ZoneInfo) -> str:
    # Model-derived text is shown only to the operator. Do not carry mention marks into the DM.
    title, chat, summary = (_plain(value.replace("@", ""))
                            for value in (event.title, event.chat_name, event.summary))
    when = event.last_sent_at.astimezone(tz)
    line = f"E-{event.id} {title}（{chat} · {when:%H:%M}）：{summary}"
    if event.flagged:
        line += "（含要求助手做事的话）"
    if event.category == "important":
        line += "（重要，未能及时告诉你）"
    return line


def _detail(text: str | None) -> str | None:
    if not text:
        return None
    return text[:_DETAIL_MAX]


def _key(day: date, channel_id: str, chat: str) -> str:
    """The brief's one key: its delivery row, its post's idempotency key and its receipt."""
    return f"morning_brief:{day.isoformat()}:{channel_id}:{chat}"


def _send_at() -> time:
    raw = chat_settings.morning_brief_time()
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        return datetime.strptime(chat_settings.DEFAULT_MORNING_BRIEF_TIME, "%H:%M").time()


async def operator_targets() -> list[tuple[str, str]]:
    """Enabled Telegram and Lark bots that name an operator, as ``(channel_id, operator_sender_id)``."""
    from ..db import channel_configs

    found: list[tuple[str, str]] = []
    for row in await channel_configs.list_configs():
        if not row.get("enabled") or row.get("channel_type") not in _BOT_TYPES:
            continue
        config = row.get("config")
        value = config.get(OPERATOR_KEY) if isinstance(config, dict) else None
        if isinstance(value, str) and value.strip():
            found.append((row["channel_id"], value.strip()))
    return found


class MorningBriefService:
    """Sends the morning brief once each workday while ``morning_brief.enabled`` is on (one in-memory read per
    tick while it is off), and not while the server drains."""

    def __init__(self, registry: Any, *, drain: Any = None, clock: Callable[[], datetime] | None = None,
                 managers: Any = None, interval: float = TICK_SECONDS) -> None:
        self._registry = registry
        self._drain = drain
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._managers = managers
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="morning-brief")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stopping.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + STOP_GRACE_SECONDS
        done, _ = await asyncio.wait({task}, timeout=STOP_GRACE_SECONDS * (1 - _CANCEL_SHARE))
        if not done:
            task.cancel()
            done, _ = await asyncio.wait({task}, timeout=max(0.0, deadline - loop.time()))
        if not done:
            logger.warning("morning brief did not stop within %s s; left to the event loop", STOP_GRACE_SECONDS)

    def _running(self) -> bool:
        return chat_settings.morning_brief_enabled() and not self._stopping.is_set() and (
            self._drain is None or not self._drain.draining)

    async def _accepts(self, channel_id: str, chat: str) -> bool:
        """The switch is still on, and ``chat`` is still this enabled channel's operator.

        The switch is read again after the operator query returns: it can be turned off while that query runs."""
        if not self._running():
            return False
        current = (channel_id, chat) in await operator_targets()
        return current and self._running()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning("morning brief failed; trying again at the next tick", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), self._interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        if (self._drain is not None and self._drain.draining) or not chat_settings.morning_brief_enabled():
            return
        async with connection.get_pool().acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", *_LOCK):
                return  # another server's tick is sending
            try:
                await self._tick(conn)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1::int, $2::int)", *_LOCK)

    async def _tick(self, conn: Any) -> None:
        now = self._clock()
        policy = proactive_delivery.load_policy()
        local = now.astimezone(policy.timezone)
        if not is_due(local, _send_at()):
            return
        day = local.date()
        if await store.orphans_uncertain(conn, now):
            logger.warning("a morning brief was being sent when its sender stopped: settled uncertain")
        targets = await operator_targets()
        if not targets:
            await store.skip(conn, day, "", "", now, "no operator direct chat")
            return
        # A date already sent, skipped or left uncertain is not read again. A refusal under the attempt limit is.
        # This is read before the summary: a day with nothing left to send does not collect.
        pending = False
        for channel_id, chat in targets:
            if await _pending(conn, day, channel_id, chat):
                pending = True
                break
        if not pending:
            return
        summary = await attention.collect(managers=self._managers)
        # The switch, the channel and the operator can change while the summary is read.
        if not self._running():
            return
        needs = attention.summary_text(summary)
        start = datetime.combine(day, time.min, tzinfo=policy.timezone)
        for channel_id, chat in await operator_targets():
            if not self._running():
                return
            if not await _pending(conn, day, channel_id, chat):
                continue
            await self._one(conn, policy, now, day, start, needs, channel_id, chat)

    async def _one(self, conn: Any, policy: Any, now: datetime, day: date, start: datetime, needs: str,
                   channel_id: str, chat: str) -> None:
        if not await self._accepts(channel_id, chat):
            return
        if not await _pending(conn, day, channel_id, chat):
            return
        reminders = await store.reminders_today(conn, channel_id=channel_id, chat=chat, start=start,
                                                end=start + timedelta(days=1))
        # The window stays the first attempt's dates through today. Rows already read are excluded by id, so a
        # custom time does not drop a message held later that morning and does not repeat one. The claim stores
        # this read's ids; a refusal records none. An insert that has not committed is not among them, so the
        # next brief still reads it, including when recovery runs after the sender stops before settle.
        origin = await store.first_attempt_date(conn, channel_id, chat) or day
        dates = list(digest_dates_through(origin, day))
        deferred = await store.digests(
            conn, channel_id=channel_id, chat=chat, until=now, dates=dates)
        triage: list[BriefEvent] = []
        triage_more = 0
        if await triage_settings.enabled(db=conn):
            dm = await operator_dm_target()
            if dm is not None and (channel_id, chat) == (dm.channel_id, dm.chat):
                dates_start = datetime.combine(min(dates), time.min, tzinfo=policy.timezone)
                triage, triage_more = await triage_events.for_brief(conn, dates_start=dates_start)
        text = render(day=day, needs=needs, reminders=reminders, digests=deferred, tz=policy.timezone,
                      triage=triage, triage_more=triage_more)
        delivery = Delivery(DeliveryKind.MORNING_BRIEF, Urgency.NORMAL, ChatKind.DIRECT, silent=text is None)
        decision = proactive_delivery.deliver(delivery, policy, DeliveryState(now))
        if not await self._accepts(channel_id, chat):
            return
        if text is None or decision.outcome is not Outcome.SEND:
            await store.skip(conn, day, channel_id, chat, now, "nothing to say")
            return
        read_ids = [row["id"] for row in deferred]
        triage_ids = [event.id for event in triage]
        if not await store.claim(conn, day, channel_id, chat, now, attempts_limit=ATTEMPTS, read_ids=read_ids,
                                 triage_ids=triage_ids):
            return
        key = _key(day, channel_id, chat)
        decided = False
        if chat_settings.delivery_enabled():
            try:
                await proactive_delivery.decide(
                    delivery, dedupe_key=key, channel_id=channel_id, chat=chat, last_user_message_at=None, now=now)
                decided = True
            except Exception:
                logger.warning("morning brief delivery decision failed; sending anyway", exc_info=True)
        if not await self._accepts(channel_id, chat):
            await store.release(conn, day, channel_id, chat)
            return
        external: str | None = None
        try:
            receipt = await self._registry.post(
                channel_id, target(chat), text, idempotency_key=key, **turn_metrics.proactive_kwargs(
                    turn_metrics.ProactiveSource.MORNING_BRIEF, dedupe_key=key))
            outcome, error = receipt.result, receipt.error
            external = receipt.external_ids[0] if receipt.external_ids else None
        except Exception as exc:
            outcome, error = PostOutcome.UNKNOWN, type(exc).__name__
        if outcome is PostOutcome.SENT:
            status = "sent"
        elif outcome is PostOutcome.REJECTED:
            status = "failed"
        else:
            status = "uncertain"
        await store.settle(conn, day, channel_id, chat, status, now=self._clock(), detail=_detail(error),
                           consumed_until=None if status == "failed" else now,
                           read_ids=None if status == "failed" else read_ids,
                           triage_ids=None if status == "failed" else triage_ids)
        if decided:
            try:
                await deliveries.record_receipt(key, outcome.value, external)
            except Exception:
                logger.warning("morning brief receipt was not recorded", exc_info=True)
        logger.info("morning brief %s %s: %s", day.isoformat(), channel_id, outcome.value)


async def _pending(conn: Any, day: date, channel_id: str, chat: str) -> bool:
    """Whether this date's brief is still to send: no row yet, or a refusal under the attempt limit."""
    row = await store.get(conn, day, channel_id, chat)
    return row is None or (row["status"] == "failed" and row["attempts"] < ATTEMPTS)
