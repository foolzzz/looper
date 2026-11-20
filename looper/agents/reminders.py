"""One-time reminders (reminders RFC): the switch, when a reminder is due, the words it is sent with, and the
service that sends it.

The user sets a reminder in their own turn (``ops.reminders``); at its time Looper sends it to the user's direct
chat with the bot it names, in words code writes (「你在 X 让我提醒你：……」), without a model. Nothing throttles it:
the user asked for it.

Each tick, under one advisory lock (so two servers never send the same reminder), the service settles as
``uncertain`` what a sender stopped in the middle of, then sends what is due (``plan``). A reminder up to
``ON_TIME`` past its time is sent as any; of the later ones of a chat (Looper was down, draining or switched off when
they came due), only the latest one due within ``CATCH_UP`` is sent, saying when it was due, and the others are
settled ``missed``. A send the channel refused is tried again at the next tick, up to ``ATTEMPTS`` times; one whose
answer was lost is ``uncertain`` and never sent again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from ..db import connection, reminders as store, system_settings
from . import cron
from .channels.base import PostOutcome
from .operator_identity import operator_sender_id
from .task_notice import NotifyTarget, TargetKind

logger = logging.getLogger(__name__)

ENABLED_KEY = "reminders.enabled"
TICK_SECONDS = 10.0
ON_TIME = timedelta(minutes=10)  # sent within this of its time, a reminder is not a missed one (a restart, a slow tick)
CATCH_UP = timedelta(hours=36)  # past ON_TIME, only a chat's latest reminder due within this is sent
LATE_NOTE = timedelta(minutes=2)  # sent this late or later, the message says when it was due
ATTEMPTS = 3  # sends the channel refused before the reminder is failed
STOP_GRACE_SECONDS = 15.0  # how long a stop waits for a send under way, so a restart does not leave it uncertain
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


def resolve_due(*, at: str | None, in_minutes: int | None, timezone: str, now: datetime) -> datetime:
    """The moment a reminder is due, from exactly one of ``at`` (``HH:MM``: the next such time in ``timezone``, today
    or tomorrow; or ``YYYY-MM-DD HH:MM`` there) and ``in_minutes`` (from ``now``). Raises ``ValueError`` saying what
    to change: a time that has passed, is more than a year ahead, or does not exist in the zone (a clock change)."""
    if (at is None) == (in_minutes is None):
        raise ValueError("give exactly one of at and in_minutes")
    tz = cron.zone(timezone)
    if in_minutes is not None:
        due = now + timedelta(minutes=in_minutes)
    else:
        text = " ".join(at.replace("T", " ").split())
        local_now = now.astimezone(tz)
        try:
            if len(text) <= 5:
                clock = datetime.strptime(text, "%H:%M")
                wall = local_now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0,
                                         tzinfo=None)
                if wall.replace(tzinfo=tz) <= now:
                    wall += timedelta(days=1)
            else:
                wall = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            raise ValueError("at is HH:MM (the next such time) or YYYY-MM-DD HH:MM, in timezone") from None
        due = wall.replace(tzinfo=tz)
        if due.astimezone(dt_timezone.utc).astimezone(tz).replace(tzinfo=None) != wall:
            raise ValueError(f"{wall:%Y-%m-%d %H:%M} does not exist in {timezone} (the clocks change then); "
                             "pick another time")
    if due <= now:
        raise ValueError(f"{due.astimezone(tz):%Y-%m-%d %H:%M} has passed: it is {now.astimezone(tz):%Y-%m-%d %H:%M}"
                         f" in {timezone} now")
    if due - now > MAX_AHEAD:
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
    if now - reminder["due_at"] >= LATE_NOTE:
        text += f"\n（原定 {reminder['due_at'].astimezone(tz):%m-%d %H:%M}，Looper 没能按时发出，现在补发。）"
    return text


@dataclass(frozen=True)
class Plan:
    """What one tick does with the due reminders: the ids it sends, and those it settles missed with why."""

    send: tuple[int, ...]
    missed: dict[int, str]


MISSED_OLD = "Looper was not sending reminders when it was due, more than 36 hours ago"
MISSED_LATER = "Looper was not sending reminders when it was due; only the latest such reminder was sent"


def plan(due: Iterable[Mapping[str, Any]], now: datetime) -> Plan:
    """Of the pending reminders due by ``now``: each one at most ``ON_TIME`` late is sent; of a chat's later ones,
    the latest one due within ``CATCH_UP`` is sent and the others are missed."""
    send: list[int] = []
    missed: dict[int, str] = {}
    chats: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for reminder in due:
        if now - reminder["due_at"] <= ON_TIME:
            send.append(reminder["id"])
        else:
            chats.setdefault((reminder["channel_id"], reminder["chat"]), []).append(reminder)
    for late in chats.values():
        late.sort(key=lambda reminder: (reminder["due_at"], reminder["id"]), reverse=True)
        latest = late[0] if now - late[0]["due_at"] <= CATCH_UP else None
        if latest is not None:
            send.append(latest["id"])
        for reminder in late:
            if reminder is not latest:
                missed[reminder["id"]] = MISSED_OLD if now - reminder["due_at"] > CATCH_UP else MISSED_LATER
    return Plan(send=tuple(send), missed=missed)


def target(chat: str) -> NotifyTarget:
    """A user's direct chat: a Lark open_id is sent to as a user, a Telegram user id is their private chat."""
    return NotifyTarget(TargetKind.USER if chat.startswith("ou_") else TargetKind.CHAT, chat)


def _clip(text: str | None) -> str | None:
    return text[:TEXT_MAX] if text else text


class ReminderService:
    """Sends the due reminders every ``interval`` seconds while ``reminders.enabled`` is on (one setting read per
    tick while it is off), and not while the server drains. A stop lets a send under way finish (up to
    ``STOP_GRACE_SECONDS``) and starts no other."""

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
        if self._task is not None:
            self._stopping.set()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), STOP_GRACE_SECONDS)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

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
        if orphans := await store.orphans_uncertain(conn, now):
            logger.warning("%d reminder(s) were being sent when their sender stopped: settled uncertain", orphans)
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
        try:
            receipt = await self._registry.post(channel_id, target(chat), message_text(reminder, now=now),
                                                idempotency_key=f"looper-reminder-{reminder_id}")
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
        logger.info("reminder %s: %s", reminder_id, outcome.value)
