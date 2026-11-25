"""One-time reminders (reminders RFC): the switch, when a reminder is due, the words it is sent with, and the
service that sends it.

The user sets a reminder in their own turn (``ops.reminders``); at its time Looper sends it to the user's direct
chat with the bot it names, in words code writes (「你在 X 让我提醒你：……」), without a model. Nothing throttles it:
the user asked for it.

Each tick, under one advisory lock (so two servers never send the same reminder), the service retries claims
that never started a post and settles started sends as ``uncertain``, then sends what is due (``plan``).
The rule holds no state:
a reminder due at most ``GRACE`` ago is sent as any; one due longer ago was missed (Looper was down, asleep, stalled,
switched off or draining), and of each chat's missed ones only the newest due within ``CATCH_UP`` is sent, saying
when it was due, and only when nothing newer goes to that chat in the same tick; the others are settled ``missed``.
A send the channel refused is tried again at the next tick, up to ``ATTEMPTS`` times; one whose answer was lost is
``uncertain`` and never sent again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from ..db import connection, proactive_deliveries as db_deliveries, reminders as store, system_settings
from . import chat_settings, cron, proactive_delivery as delivery
from .channels.base import PostOutcome
from .operator_identity import operator_sender_id
from .task_notice import NotifyTarget, TargetKind

logger = logging.getLogger(__name__)

ENABLED_KEY = "reminders.enabled"
TICK_SECONDS = 10.0
GRACE = timedelta(minutes=2)  # due at most this long ago, a reminder is on time (12 ticks; a restart, a slow tick)
CATCH_UP = timedelta(hours=36)  # of a chat's missed reminders, only the newest due within this is sent
ATTEMPTS = 3  # sends the channel refused before the reminder is failed
STOP_GRACE_SECONDS = 15.0  # how long a stop takes at most: the send under way, then cancelling it
_CANCEL_SHARE = 0.25  # of STOP_GRACE_SECONDS, kept for cancelling a send that did not finish
MAX_AHEAD = timedelta(days=366)
TEXT_MAX = 500
_LOCK = (741925, 1)  # (namespace, key) of the session advisory lock a tick holds
_WEEKDAYS = "一二三四五六日"


async def enabled(db: Any = None) -> bool:
    """Whether reminders are on (``reminders.enabled`` is ``true``; no row is off)."""
    stored = await system_settings.get_settings([ENABLED_KEY], pool=db)
    return stored.get(ENABLED_KEY) == "true"


async def reminders_on() -> bool:
    """``enabled()`` for what Looper offers a model: off when the switch cannot be read (no database)."""
    try:
        return await enabled()
    except Exception:
        logger.debug("could not read reminders.enabled; reminders count as off", exc_info=True)
        return False


def default_timezone() -> str:
    return cron.local_zone_name()


@dataclass(frozen=True)
class Due:
    """When a reminder is due, and what the user is told about a clock change that moved or chose the time."""

    at: datetime
    note: str | None = None


def _instants(wall: datetime, timezone: str) -> list[Due]:
    """The instants a wall-clock time names in ``timezone``: one; two when the clocks go back (each with a note
    saying which it is); or, when the clocks skip it, the first valid minute after it, with a note."""
    tz = cron.zone(timezone)
    found: list[datetime] = []
    for fold in (0, 1):
        moment = wall.replace(tzinfo=tz, fold=fold).astimezone(dt_timezone.utc)
        if moment.astimezone(tz).replace(tzinfo=None) == wall and moment not in found:
            found.append(moment)
    if len(found) == 2:
        return [Due(moment, f"{wall:%m-%d %H:%M} 在 {timezone} 有两次（夏令时结束），记的是{which}"
                            f"（{moment.astimezone(tz):%Z}）")
                for moment, which in zip(sorted(found), ("第一次", "第二次"))]
    if found:
        return [Due(found[0])]
    after = wall
    for _ in range(2 * 24 * 60):  # a gap is an hour or so; a skipped day is the longest there has been
        after += timedelta(minutes=1)
        moment = after.replace(tzinfo=tz).astimezone(dt_timezone.utc)
        if moment.astimezone(tz).replace(tzinfo=None) == after:
            return [Due(moment, f"{wall:%m-%d %H:%M} 在 {timezone} 不存在（夏令时开始），改为 {after:%m-%d %H:%M}")]
    raise ValueError(f"{wall:%Y-%m-%d %H:%M} does not exist in {timezone}; pick another time")


def resolve_due(*, at: str | None, in_minutes: int | None, timezone: str, now: datetime) -> Due:
    """When a reminder is due, from exactly one of ``at`` (``HH:MM``: the next such time in ``timezone``; or
    ``YYYY-MM-DD HH:MM`` there) and ``in_minutes`` (from ``now``): the nearest future instant the wall time names.
    When the clocks go back it is the first of the two that is still ahead; when they skip it, the first valid minute
    after it; ``Due.note`` says so. Raises ``ValueError`` saying what to change: a time that has passed or is more
    than a year ahead, or a malformed one."""
    if (at is None) == (in_minutes is None):
        raise ValueError("give exactly one of at and in_minutes")
    tz = cron.zone(timezone)
    if in_minutes is not None:
        due = Due(now + timedelta(minutes=in_minutes))
    else:
        text = " ".join(at.replace("T", " ").split())
        try:
            if len(text) <= 5:
                clock = datetime.strptime(text, "%H:%M")
                today = now.astimezone(tz).replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0,
                                                   tzinfo=None, fold=0)
                walls = [today + timedelta(days=days) for days in range(3)]
            else:
                walls = [datetime.strptime(text, "%Y-%m-%d %H:%M")]
        except ValueError:
            raise ValueError("at is HH:MM (the next such time) or YYYY-MM-DD HH:MM, in timezone") from None
        ahead = [due for wall in walls for due in _instants(wall, timezone) if due.at > now]
        if not ahead:
            raise ValueError(f"{walls[0]:%Y-%m-%d %H:%M} has passed: it is {now.astimezone(tz):%Y-%m-%d %H:%M} in "
                             f"{timezone} now")
        due = min(ahead, key=lambda candidate: candidate.at)
    if due.at <= now:
        raise ValueError("in_minutes is at least 1")
    if due.at - now > MAX_AHEAD:
        raise ValueError("a reminder is at most a year ahead")
    return due


def when(moment: datetime, timezone: str, *, now: datetime | None = None) -> str:
    """``moment`` in ``timezone`` as the user reads it: 「11-20（周五）15:00」, with 今天 or 明天 before it when ``now``
    makes it so."""
    tz = cron.zone(timezone)
    local = moment.astimezone(tz)
    text = f"{local:%m-%d}（周{_WEEKDAYS[local.weekday()]}）{local:%H:%M}"
    if now is not None:
        days = (local.date() - now.astimezone(tz).date()).days
        text = {0: "今天 ", 1: "明天 "}.get(days, "") + text
    return text


def message_text(reminder: Mapping[str, Any], *, now: datetime) -> str:
    """What the user gets at the time: their own words, after when they asked; sent late, when it was due."""
    tz = cron.zone(reminder["timezone"])
    asked = reminder["created_at"].astimezone(tz)
    text = f"⏰ 你在 {asked:%m-%d %H:%M} 让我提醒你：{reminder['text']}"
    if now - reminder["due_at"] > GRACE:
        text += f"\n（原定 {reminder['due_at'].astimezone(tz):%m-%d %H:%M}，Looper 没能按时发出，现在补发。）"
    return text


@dataclass(frozen=True)
class Plan:
    """What one tick does with the due reminders: the ids it sends, and those it settles missed with why."""

    send: tuple[int, ...]
    missed: dict[int, str]


MISSED_OLD = "Looper was not sending reminders when it was due, more than 36 hours ago"
MISSED_LATER = "Looper was not sending reminders when it was due; only the newest reminder of this chat was sent"


def plan(due: Iterable[Mapping[str, Any]], now: datetime) -> Plan:
    """Of the pending reminders due by ``now``: each one due at most ``GRACE`` ago is sent, however many at once.
    The others were missed: of a chat's missed ones, the newest due within ``CATCH_UP`` is sent unless something
    on time goes to that chat in this tick (that one is newer), and the rest are missed. No state: a start, a sleep,
    a stall and two servers taking turns are judged alike."""
    send: list[int] = []
    missed: dict[int, str] = {}
    chats: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    on_time: set[tuple[str, str]] = set()
    for reminder in due:
        chat = (reminder["channel_id"], reminder["chat"])
        if now - reminder["due_at"] <= GRACE:
            send.append(reminder["id"])
            on_time.add(chat)
        else:
            chats.setdefault(chat, []).append(reminder)
    for chat, late in chats.items():
        late.sort(key=lambda reminder: (reminder["due_at"], reminder["id"]), reverse=True)
        newest = late[0] if chat not in on_time and now - late[0]["due_at"] <= CATCH_UP else None
        if newest is not None:
            send.append(newest["id"])
        for reminder in late:
            if reminder is not newest:
                missed[reminder["id"]] = MISSED_OLD if now - reminder["due_at"] > CATCH_UP else MISSED_LATER
    return Plan(send=tuple(send), missed=missed)


def target(chat: str) -> NotifyTarget:
    """A user's direct chat: a Lark open_id is sent to as a user, a Telegram user id is their private chat."""
    return NotifyTarget(TargetKind.USER if chat.startswith("ou_") else TargetKind.CHAT, chat)


def _clip(text: str | None) -> str | None:
    return text[:TEXT_MAX] if text else text


class ReminderService:
    """Sends the due reminders every ``interval`` seconds while ``reminders.enabled`` is on (one setting read per
    tick while it is off), and not while the server drains. A stop starts no other send and takes at most
    ``STOP_GRACE_SECONDS``: the send under way may finish in most of it, and is cancelled in the rest."""

    def __init__(self, registry: Any, *, drain: Any = None, clock: Callable[[], datetime] | None = None,
                 interval: float = TICK_SECONDS) -> None:
        self._registry = registry
        self._drain = drain
        self._clock = clock or (lambda: datetime.now(dt_timezone.utc))
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="reminders")

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
            logger.warning("reminder delivery did not stop within %s s; left to the event loop", STOP_GRACE_SECONDS)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.warning("reminder delivery failed; trying again at the next tick", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), self._interval)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        if (self._drain is not None and self._drain.draining) or not await enabled():
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
        retried, uncertain = await store.recover(conn, now)
        if retried or uncertain:
            logger.warning("recovered reminders: %d unstarted claim(s) pending, %d started send(s) uncertain",
                           retried, uncertain)
        due = await store.due(conn, now)
        decided = plan(due, now)
        for reminder_id, why in decided.missed.items():
            await store.settle(conn, reminder_id, "missed", now=now, detail=why, from_status="pending")
            logger.info("reminder %s missed: %s", reminder_id, why)
        by_id = {reminder["id"]: reminder for reminder in due}
        for reminder_id in decided.send:
            if self._stopping.is_set() or (self._drain is not None and self._drain.draining):
                return
            await self._send(conn, by_id[reminder_id])

    async def _send(self, conn: Any, reminder: Mapping[str, Any]) -> None:
        reminder_id, channel_id, chat = reminder["id"], reminder["channel_id"], reminder["chat"]
        now = self._clock()
        if await operator_sender_id(channel_id) != chat:
            # The bot's operator is someone else now (or no one): never send the user's words to them.
            await store.settle(conn, reminder_id, "failed", now=now, from_status="pending",
                               detail=f"{channel_id} no longer names this user as its operator")
            logger.warning("reminder %s failed: %s no longer names its user as operator", reminder_id, channel_id)
            return
        if not await store.claim(conn, reminder_id, now):
            return  # cancelled meanwhile
        key = f"looper-reminder-{reminder_id}"
        decided = None
        if chat_settings.delivery_enabled():
            try:
                # User-set reminders bypass quiet hours, the daily cap and the digest (reminders RFC §5).
                decided = await delivery.decide(
                    delivery.Delivery(delivery.DeliveryKind.USER_REMINDER, delivery.Urgency.USER_SET,
                                      delivery.ChatKind.DIRECT),
                    dedupe_key=key, channel_id=channel_id, chat=chat, source_ref=str(reminder_id),
                    last_user_message_at=None, now=now)
            except Exception:
                logger.exception("reminder %s: decision failed; sending as with the layer off", reminder_id)
        await store.start_post(conn, reminder_id, self._clock())
        try:
            receipt = await self._registry.post(channel_id, target(chat), message_text(reminder, now=now),
                                                idempotency_key=key)
            outcome, error = receipt.result, receipt.error
            external = receipt.external_ids[0] if receipt.external_ids else None
        except Exception as exc:
            outcome, error, external = PostOutcome.UNKNOWN, type(exc).__name__, None
        settled_at = self._clock()
        if outcome is PostOutcome.SENT:
            await store.settle(conn, reminder_id, "delivered", now=settled_at, external_id=external)
        elif outcome is PostOutcome.REJECTED and reminder["attempts"] + 1 < ATTEMPTS:
            await store.release(conn, reminder_id, now=settled_at, error=_clip(error))
        elif outcome is PostOutcome.REJECTED:
            await store.settle(conn, reminder_id, "failed", now=settled_at, detail=_clip(error))
        else:
            await store.settle(conn, reminder_id, "uncertain", now=settled_at, detail=_clip(error))
        if decided is not None:
            try:
                await db_deliveries.record_receipt(key, outcome.value, external)
            except Exception:
                # A recording failure must not retry an accepted or uncertain channel send.
                logger.exception("reminder %s: could not record the channel's receipt", reminder_id)
        logger.info("reminder %s: %s", reminder_id, outcome.value)
