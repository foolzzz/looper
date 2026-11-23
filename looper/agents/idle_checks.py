"""Idle checks (assistant phase 3 RFC, section 4): when the default Manager has been idle for a while, code reads the
needs-you summary, picks what is new since it last looked, and only then asks the Manager (a read-only turn in
``main``) whether to say something. The Manager may answer ``NO_REPLY``; what it writes goes to the operator's direct
chat through the delivery layer.

Three layers, each deciding one thing, each recorded:

- code decides whether to look at all (``plan``: a pure function of typed inputs, so every rule has a table test)
  and what is new (``select``: per section rules and fingerprints);
- the Manager decides whether to speak (``parse_reply``: ``NO_REPLY`` exactly, or a message);
- the delivery layer decides whether it goes out now (``deliver_result``).

``IdleCheckService`` runs the first layer every ``TICK_SECONDS``. It writes one ``idle_checks`` row (``running``) and
wakes the Manager with ``IDLE_CHECK_DUE``; the turn itself is the Manager's. A tick that finds nothing to do writes
nothing: it only counts the reason and logs it at DEBUG. With ``manager.idle_checks.enabled`` off (the default) a tick
reads the switch and stops.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone as dt_timezone, tzinfo
from enum import Enum, StrEnum
from typing import Any

from ..db import connection, idle_checks as store, proactive_deliveries as db_deliveries
from . import attention, idle_check_settings, proactive_delivery as delivery, turn_context, untrusted
from .channels.base import PostOutcome
from .managers_api import DEFAULT_MANAGER_ID
from .message import MessageEnvelope, MessageType, channel_thread_id, manager_address, parse_channel_id
from .operator_identity import DmTarget, operator_dm_target
from .reminders import target as notify_target
from .turn_metrics import ProactiveSource

logger = logging.getLogger(__name__)

MANAGER_ID = DEFAULT_MANAGER_ID
TICK_SECONDS = 300.0
IDLE_MINUTES = 30  # idle at least this long
MIN_GAP = timedelta(hours=2)  # between the starts of two checks
MAX_PER_DAY = 4  # checks that begin on one local date
MAX_ITEMS = 10  # items one check names
MENTIONED_WINDOW = timedelta(days=7)
INTENT_WAIT = timedelta(hours=2)  # an intent is shown to the operator for this long before a check names it
LEDGER_SOON = timedelta(hours=24)
IDLE_TURN_SECONDS = 90.0  # the turn's deadline (the Manager's shared background-turn entry)
NO_REPLY = "NO_REPLY"
DETAILS_LINE = "详情：发 /todo，或打开控制台的 Needs you。"
REPLY_MAX = 2000  # ``idle_checks.reply``
FAILING_RUN = 3  # this many failed checks in a row are worth a warning
_LOCK = (741925, 2)  # (namespace, key) of the session advisory lock "decide, insert, publish" holds
_EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
_LOGGED_ONCE_A_DAY = frozenset({"no_target", "lanes_off"})


# --- The plan: whether to look, from typed inputs ---------------------------------------------------------------


class Unread(Enum):
    """An input of ``plan`` that costs a database read and is not read yet."""

    UNREAD = "unread"


UNREAD = Unread.UNREAD


class Skip(StrEnum):
    """Why a tick starts no check (the counter's and the DEBUG log's label)."""

    OFF = "off"
    LANES_OFF = "lanes_off"
    DELIVERY_OFF = "delivery_off"
    ATTENTION_OFF = "attention_off"
    MANAGER_OFF = "manager_off"
    DRAINING = "draining"
    NOT_IDLE = "not_idle"
    IDLE_TOO_SHORT = "idle_too_short"
    QUIET_HOURS = "quiet_hours"
    DAY_LIMIT = "day_limit"
    TOO_SOON = "too_soon"
    MORNING_BRIEF_RECENT = "morning_brief_recent"
    NO_TARGET = "no_target"
    DM_CAP_FULL = "dm_cap_full"
    NO_NEW_ITEMS = "no_new_items"


class Need(StrEnum):
    """The next read ``plan`` asks for, cheapest first."""

    COUNTS = "counts"  # today's checks and when the last began
    TARGET = "target"  # the operator's direct chat
    MORNING_BRIEF = "morning_brief"  # when this target last received its morning brief
    DM_COUNT = "dm_count"  # what that chat was sent today
    ITEMS = "items"  # the new items


class Action(StrEnum):
    SKIP = "skip"
    READ = "read"
    CHECK = "check"


@dataclass(frozen=True)
class IdleState:
    """What ``plan`` judges. The first fields are in memory or one setting read; the rest are ``UNREAD`` until the
    plan asks for them (``Need``)."""

    switch: bool
    lanes: bool
    delivery: bool
    attention: bool
    manager_running: bool
    draining: bool
    idle_seconds: float | None  # None: ``main`` is busy
    quiet: bool  # inside the delivery layer's quiet hours
    dm_cap: int = delivery.DEFAULT_DM_DAILY_CAP
    checks_today: int | Unread = UNREAD
    since_last_check: float | None | Unread = UNREAD  # seconds; None: no check before
    target: DmTarget | None | Unread = UNREAD
    since_morning_brief: float | None | Unread = UNREAD
    dm_counted: int | Unread = UNREAD
    new_items: int | Unread = UNREAD


@dataclass(frozen=True)
class IdlePlan:
    action: Action
    skip: Skip | None = None
    need: Need | None = None


def _skip(reason: Skip) -> IdlePlan:
    return IdlePlan(Action.SKIP, skip=reason)


def _read(need: Need) -> IdlePlan:
    return IdlePlan(Action.READ, need=need)


def plan(state: IdleState) -> IdlePlan:
    """The rules of RFC 4.3, in order, the cheap ones first. ``skip`` says which one stopped it; ``read`` that
    every rule before it holds and it needs one more input; ``check`` that all hold. It reads no database and no
    clock."""
    for ok, reason in ((state.switch, Skip.OFF), (state.lanes, Skip.LANES_OFF), (state.delivery, Skip.DELIVERY_OFF),
                       (state.attention, Skip.ATTENTION_OFF), (state.manager_running, Skip.MANAGER_OFF),
                       (not state.draining, Skip.DRAINING)):
        if not ok:
            return _skip(reason)
    if state.idle_seconds is None:
        return _skip(Skip.NOT_IDLE)
    if state.idle_seconds < IDLE_MINUTES * 60:
        return _skip(Skip.IDLE_TOO_SHORT)
    if state.quiet:
        return _skip(Skip.QUIET_HOURS)
    if state.checks_today is UNREAD or state.since_last_check is UNREAD:
        return _read(Need.COUNTS)
    if state.checks_today >= MAX_PER_DAY:
        return _skip(Skip.DAY_LIMIT)
    if state.since_last_check is not None and state.since_last_check < MIN_GAP.total_seconds():
        return _skip(Skip.TOO_SOON)
    if state.target is UNREAD:
        return _read(Need.TARGET)
    if state.target is None:
        return _skip(Skip.NO_TARGET)
    if state.since_morning_brief is UNREAD:
        return _read(Need.MORNING_BRIEF)
    if state.since_morning_brief is not None and state.since_morning_brief < MIN_GAP.total_seconds():
        return _skip(Skip.MORNING_BRIEF_RECENT)
    if state.dm_counted is UNREAD:
        return _read(Need.DM_COUNT)
    if state.dm_counted >= state.dm_cap:
        return _skip(Skip.DM_CAP_FULL)
    if state.new_items is UNREAD:
        return _read(Need.ITEMS)
    if not state.new_items:
        return _skip(Skip.NO_NEW_ITEMS)
    return IdlePlan(Action.CHECK)


# --- Candidates: what is new, by section ------------------------------------------------------------------------


def _iso(moment: Any) -> str | None:
    if not isinstance(moment, datetime):
        return None
    return moment.astimezone(dt_timezone.utc).isoformat()


def _plain_json(value: Any) -> Any:
    """A fact as JSON keeps it: moments as UTC ISO text, containers recursively."""
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, Mapping):
        return {str(key): _plain_json(inner) for key, inner in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(inner) for inner in value]
    return value


@dataclass(frozen=True)
class SectionRule:
    """A section's rule: whether an item of it is a candidate (the raw summary item, now) and its fingerprint (the
    stored item: the same text from the same item, a different one when the item changed state)."""

    include: Callable[[Mapping[str, Any], datetime], bool]
    fingerprint: Callable[[Mapping[str, Any]], tuple[Any, ...]]


def _ledger_due(item: Mapping[str, Any], now: datetime) -> bool:
    facts = item["facts"]
    due = facts.get("due_at")
    return bool(facts.get("overdue")) or (isinstance(due, datetime) and due <= now + LEDGER_SOON)


def _intent_waited(item: Mapping[str, Any], now: datetime) -> bool:
    since = item.get("since")
    return isinstance(since, datetime) and now - since >= INTENT_WAIT


# One row per section; ``None`` is a section that never gives a candidate (and why).
RULES: dict[str, SectionRule | None] = {
    "gates": None,  # a gate has its own reminder (24 h by default)
    "confirmations": None,  # void after ten minutes: not there after thirty idle ones
    "schedules": SectionRule(
        lambda item, now: True,  # the summary's own rule: failures in a row, or an alert
        # ``last_ok_at``, not ``last_success_at``: a gate that found nothing to do ends a streak and writes the
        # first, not the second, so "fail, skipped_gate, fail" is a new failure.
        lambda item: (item["key"], item["since"], item["facts"].get("last_ok_at"))),
    "logins": SectionRule(lambda item, now: True, lambda item: (item["key"],)),
    "intents": SectionRule(_intent_waited, lambda item: (item["key"], item["since"])),
    "ledger": SectionRule(_ledger_due,
                          lambda item: (item["key"], item["since"], bool(item["facts"].get("overdue")))),
    "deliveries": SectionRule(lambda item, now: True, lambda item: (item["key"], item["since"])),
}


def fingerprint(item: Mapping[str, Any]) -> str:
    """The fingerprint of a stored item (``{"section", "key", "since", "title", "facts"}``)."""
    rule = RULES[item["section"]]
    assert rule is not None, f"{item['section']} gives no candidates"
    return json.dumps([item["section"], *rule.fingerprint(item)], ensure_ascii=False)


def _stored(section: str, item: Mapping[str, Any]) -> dict[str, Any]:
    facts = _plain_json(item["facts"])
    for name in ("context", "ref"):
        if item.get(name):
            facts[name] = attention._plain(item[name]) if name == "context" else item[name]
    return {"section": section, "key": item["key"], "since": _iso(item.get("since")),
            "title": attention._plain(item["title"]), "facts": facts}


def select(summary: Mapping[str, Any], *, now: datetime, mentioned: Sequence[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The items to name in a check: those of the summary the section rules let in, whose fingerprint no check of
    the last days mentioned, oldest ``since`` first (undated ones last), at most ``MAX_ITEMS``. A section that is
    ``error`` or ``unknown`` gives none; the others are read as usual."""
    seen = {fingerprint(item) for item in mentioned if item.get("section") in RULES and RULES[item["section"]]}
    found: list[tuple[bool, datetime, dict[str, Any]]] = []
    for section in summary["sections"]:
        rule = RULES.get(section["kind"])
        if rule is None or section["state"] != "ok":
            continue
        for item in section["items"]:
            if not rule.include(item, now):
                continue
            stored = _stored(section["kind"], item)
            if (print_ := fingerprint(stored)) in seen:
                continue
            seen.add(print_)
            since = _moment(stored["since"])
            found.append((since is None, since or _EPOCH, stored))
    found.sort(key=lambda entry: entry[:2])  # dated by age, then the undated (a login has no clock)
    return [entry[2] for entry in found[:MAX_ITEMS]]


# --- The message the Manager reads ------------------------------------------------------------------------------

_LEAD = ("[IDLE CHECK] Looper looked for what may need the operator while you were idle. This is not a message from "
         "the user, and nobody asked you anything.")
_TAIL = (
    "Reply with exactly NO_REPLY when none of this needs the operator now: they already know (it came up in this "
    "conversation), it can wait for their morning brief, or it is not theirs to act on.\n"
    "Otherwise write one message to the operator, in their language:\n"
    "- At most 4 lines: what needs them and the one thing they can do (a command, a page, a word to you). No "
    "greeting, no headings, no list of everything above.\n"
    "- In this turn you cannot act: your tools only read, and a change answers needs_user_turn. Never say you did, "
    "started or fixed anything.\n"
    "- Read with your tools only when the lines above are not enough.")


def _fenced(*parts: str) -> str:
    return untrusted.fence(" — ".join(part for part in parts if part), source=untrusted.Source.NEEDS_YOU)


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _ago(since: datetime | None, now: datetime) -> str:
    if since is None:
        return ""
    minutes = max(0, int((now - since).total_seconds() // 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h"


def _at(moment: datetime | None, now: datetime, zone: tzinfo) -> str:
    if moment is None:
        return ""
    local, today = moment.astimezone(zone), now.astimezone(zone)
    return f"{local:%H:%M}" if local.date() == today.date() else f"{local:%m-%d %H:%M}"


def _schedule_line(item: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    facts = item["facts"]
    parts = []
    if facts.get("failures"):
        parts.append(f"{facts['failures']} failures in a row")
    if facts.get("alerts"):
        parts.append("alerts: " + ", ".join(facts["alerts"]))
    since = _at(_moment(item["since"]), now, zone)
    detail = "; ".join(parts) + (f", since {since}" if since else "") if parts else "it failed"
    return f"A schedule is failing: {detail}. Its name and project: {_fenced(item['title'], facts.get('context', ''))}"


def _login_line(item: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    fix = attention._plain(item["facts"].get("fix"))
    return f"A login fails: {attention._plain(item['title'])}." + (f" Fix: {fix}" if fix else "")


def _intent_line(item: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    waited = _ago(_moment(item["since"]), now)
    return (f"An intent has waited {waited} to be built or rejected: {item['facts'].get('ref', '')} "
            f"{_fenced(item['title'])}").replace("  ", " ")


def _ledger_line(item: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    facts = item["facts"]
    what = "overdue" if facts.get("overdue") else f"due {_at(_moment(facts.get('due_at')), now, zone)}"
    return f"A ledger item waits for the operator ({what}): {facts.get('ref', '')} {_fenced(item['title'])}".replace(
        "  ", " ")


def _delivery_line(item: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    facts = item["facts"]
    since = _ago(_moment(item["since"]), now)
    return (f"A message may not have arrived ({facts.get('source', 'message')}"
            f"{', ' + since + ' ago' if since else ''}); it was not sent again: {_fenced(item['title'])}")


_LINES: dict[str, Callable[[Mapping[str, Any], datetime, tzinfo], str]] = {
    "schedules": _schedule_line, "logins": _login_line, "intents": _intent_line, "ledger": _ledger_line,
    "deliveries": _delivery_line}


def _zone_label(zone: tzinfo, now: datetime) -> str:
    offset = now.astimezone(zone).utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    name = getattr(zone, "key", None) or now.astimezone(zone).tzname() or "local"
    return f"{name}, UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def render_message(items: Sequence[Mapping[str, Any]], *, now: datetime, zone: tzinfo | None = None) -> str:
    """The ``[IDLE CHECK]`` message for the stored ``items``. English, written by code. Every title others wrote goes
    through ``attention._plain`` (one plain line) and then ``untrusted.fence``, whatever ``untrusted_fence.enabled``
    says: this text is new, so there are no earlier bytes to keep."""
    zone = zone or turn_context.zone()
    local = now.astimezone(zone)
    lines = [f"{number}. {_LINES[item['section']](item, now, zone)}" for number, item in enumerate(items, 1)]
    return "\n".join([
        _LEAD,
        f"Local time: {local:%Y-%m-%d %a %H:%M} ({_zone_label(zone, now)})",
        untrusted.LEAD_LINE,
        f"New since your last check ({len(items)}), written by Looper from its records:",
        *lines, "", _TAIL])


# --- The reply ----------------------------------------------------------------------------------------------------


class IdleReply(StrEnum):
    SILENT = "silent"
    MESSAGE = "message"
    INVALID = "invalid"


def parse_reply(text: str) -> IdleReply:
    """``NO_REPLY`` and nothing else (spaces around it aside) is ``SILENT``; text without ``NO_REPLY`` is a
    ``MESSAGE``; text that mixes them, and an empty reply, is ``INVALID`` (nothing is sent). No guessing at
    punctuation or case."""
    stripped = (text or "").strip()
    if stripped == NO_REPLY:
        return IdleReply.SILENT
    if not stripped or NO_REPLY in stripped:
        return IdleReply.INVALID
    return IdleReply.MESSAGE


# --- What the turn ends in ----------------------------------------------------------------------------------------

PostFn = Callable[..., Awaitable[Any]]


def _clip(text: str, limit: int = REPLY_MAX) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def settle(check_id: int, state: str, *, reason: str | None = None, reply: str | None = None,
                 record_id: int | None = None, manager_id: str = MANAGER_ID) -> bool:
    """End a check (``store.settle``), and warn when the last ``FAILING_RUN`` checks all failed."""
    done = await store.settle(check_id, state, reason=reason, reply=None if reply is None else _clip(reply),
                              record_id=record_id)
    if done and state == "failed":
        try:
            if _failed_run(await store.last_states(manager_id, FAILING_RUN)):
                logger.warning("idle checks failing: the last %d checks all failed", FAILING_RUN)
        except Exception:
            logger.debug("could not read the last idle checks", exc_info=True)
    return done


def _failed_run(states: Sequence[str]) -> bool:
    return len(states) >= FAILING_RUN and all(state == "failed" for state in states[:FAILING_RUN])


async def _last_user_message_at(channel_id: str, chat: str) -> datetime | None:
    thread = channel_thread_id(channel_id, chat, "manager", "manager")
    try:
        return await connection.get_pool().fetchval(
            "SELECT max(created_at) FROM chat_messages WHERE session_id = $1 AND role = 'user'", thread)
    except Exception:
        logger.warning("idle checks: could not read the last user message", exc_info=True)
        return None


async def _remember(check: Mapping[str, Any], text: str, *, failure: str | None = None) -> None:
    """Keep what was said in the operator's direct chat, as a completion reply is kept, so a reply to it has its
    context. A storage failure is logged: the message is out."""
    from ..db import chat as db_chat

    parsed = parse_channel_id(check["channel_id"])
    if parsed is None:
        return
    try:
        await db_chat.save_channel_message(
            session_id=channel_thread_id(check["channel_id"], check["chat"], "manager", "manager"),
            message_id=f"idle-{check['id']}", role="assistant", content=text, channel=parsed.channel_type,
            manager_id=MANAGER_ID, delivery_failure=failure)
    except Exception:
        logger.warning("could not store idle check %s's message in the chat", check["id"], exc_info=True)


async def deliver_result(check: Mapping[str, Any], reply: str, *, post: PostFn, record_id: int | None = None,
                         now: datetime | None = None, cancelled: Callable[[], bool] | None = None) -> str:
    """End a ``running`` check with the Manager's ``reply``: ``silent``; ``failed`` (``invalid_reply``,
    ``delivery_failed``, ``send_rejected``); ``deferred`` when the delivery layer says digest; or ``sent`` /
    ``uncertain`` after the send (``post``: ``ChannelRegistry.post``). Answers the state the check ended in."""
    check_id, channel_id, chat = check["id"], check["channel_id"], check["chat"]
    kind = parse_reply(reply)
    if kind is IdleReply.SILENT:
        await settle(check_id, "silent", reply=NO_REPLY, record_id=record_id)
        return "silent"
    if kind is IdleReply.INVALID:
        await settle(check_id, "failed", reason="invalid_reply", reply=reply, record_id=record_id)
        return "failed"
    key = f"idle-{check_id}"
    spoke = await _last_user_message_at(channel_id, chat)
    if spoke is not None and spoke > check["created_at"]:
        await settle(check_id, "preempted", record_id=record_id)
        return "preempted"
    try:
        decided = await delivery.decide(
            delivery.Delivery(delivery.DeliveryKind.IDLE_CHECK, delivery.Urgency.NORMAL, delivery.ChatKind.DIRECT),
            dedupe_key=key, channel_id=channel_id, chat=chat,
            last_user_message_at=spoke, source_ref=str(check_id),
            manager_id=check["manager_id"], now=now)
    except Exception:  # not urgent: unlike a gate's reminder, it is not sent without the layer's say
        logger.warning("idle check %s: the delivery decision failed", check_id, exc_info=True)
        await settle(check_id, "failed", reason="delivery_failed", reply=reply, record_id=record_id)
        return "failed"
    if cancelled is not None and cancelled():
        await settle(check_id, "preempted", record_id=record_id)
        return "preempted"
    if decided.outcome is not delivery.Outcome.SEND:
        await settle(check_id, "deferred", reply=reply, record_id=record_id)
        return "deferred"
    spoke = await _last_user_message_at(channel_id, chat)
    if spoke is not None and spoke > check["created_at"]:
        await settle(check_id, "preempted", record_id=record_id)
        return "preempted"
    text = f"{_clip(reply.strip(), REPLY_MAX - len(DETAILS_LINE) - 2)}\n\n{DETAILS_LINE}"
    external: str | None = None
    if not await store.claim_sending(check_id):
        return (await store.get(check_id) or {}).get("state", "failed")
    try:
        receipt = await post(channel_id, notify_target(chat), text, idempotency_key=key)
        outcome, external = receipt.result, receipt.external_ids[0] if receipt.external_ids else None
    except Exception:
        logger.warning("idle check %s: the send raised; it may have gone", check_id, exc_info=True)
        outcome = PostOutcome.UNKNOWN
    try:  # the delivery's receipt, on the decided row (as the intent push writes it)
        await db_deliveries.insert({
            "dedupe_key": key, "source": ProactiveSource.IDLE_CHECK.value, "source_ref": str(check_id),
            "manager_id": check["manager_id"], "channel_id": channel_id, "chat": chat, "chat_kind": "direct",
            "external_id": external,
            "outcome": {PostOutcome.SENT: "sent", PostOutcome.REJECTED: "rejected",
                        PostOutcome.UNKNOWN: "unknown"}[outcome]})
    except Exception:
        logger.warning("idle check %s: its delivery receipt was not recorded", check_id, exc_info=True)
    if outcome is PostOutcome.REJECTED:
        await settle(check_id, "failed", reason="send_rejected", reply=reply, record_id=record_id)
        return "failed"
    await _remember(check, text)
    state = "sent" if outcome is PostOutcome.SENT else "uncertain"
    await settle(check_id, state, reply=reply, record_id=record_id)
    return state


def due_envelope(check_id: int) -> MessageEnvelope:
    """The wake-up for check ``check_id``: from and to the default Manager, a hint only (the row is the truth)."""
    source = manager_address(MANAGER_ID)
    return MessageEnvelope.create(source=source, target=source, message_type=MessageType.IDLE_CHECK_DUE,
                                  payload={"check_id": check_id}, correlation_id=f"idle-check-{check_id}")


# --- The service ----------------------------------------------------------------------------------------------------


def _quiet(policy: delivery.DeliveryPolicy, now: datetime) -> bool:
    return policy.quiet_hours is not None and policy.quiet_hours.contains(now.astimezone(policy.timezone).time())


class IdleCheckService:
    """Looks every ``interval`` seconds, and starts a check when ``plan`` says so (the module docstring). One
    server's tick at a time holds the session advisory lock around "decide, insert, publish": the turn is the
    Manager's and holds nothing here.

    ``idle_for`` is the Manager's read-only access to ``main`` (seconds since its last turn ended, None when it is
    busy); ``manager_running`` says whether the default Manager runs."""

    def __init__(self, bus: Any, *, idle_for: Callable[[], float | None], manager_running: Callable[[], bool],
                 drain: Any = None, clock: Callable[[], datetime] | None = None,
                 interval: float = TICK_SECONDS) -> None:
        self._bus = bus
        self._idle_for = idle_for
        self._manager_running = manager_running
        self._drain = drain
        self._clock = clock or (lambda: datetime.now(dt_timezone.utc))
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.skips: Counter[Skip] = Counter()
        self._logged: dict[Skip, date] = {}

    async def recover(self) -> None:
        """Recover interrupted sends before failing running checks and replaying wakes; writes must succeed."""
        sending = await store.recover_sending(manager_id=MANAGER_ID)
        for row in sending:
            await db_deliveries.insert({
                "dedupe_key": f"idle-{row['id']}", "source": ProactiveSource.IDLE_CHECK.value,
                "source_ref": str(row["id"]), "manager_id": row["manager_id"],
                "channel_id": row["channel_id"], "chat": row["chat"], "chat_kind": "direct",
                "outcome": "unknown"})
        if sending:
            logger.info("idle checks: %d check(s) left sending recovered as uncertain", len(sending))
        if count := await store.settle_running("restart", manager_id=MANAGER_ID):
            logger.info("idle checks: %d check(s) left running by the server before this one failed (restart)", count)

    async def start(self) -> None:
        """Start ticks after recovery and the Manager consumers are ready."""
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="idle-checks")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stopping.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("idle checks: a tick failed; trying again at the next one", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), self._interval)
            except asyncio.TimeoutError:
                pass

    def _skipped(self, reason: Skip, now: datetime) -> None:
        self.skips[reason] += 1
        today = now.date()
        if reason in _LOGGED_ONCE_A_DAY and self._logged.get(reason) != today:
            self._logged[reason] = today
            logger.info("idle checks: not checking (%s)", reason.value)
        else:
            logger.debug("idle checks: not checking (%s)", reason.value)

    async def run_once(self) -> int | None:
        """One tick. The id of the check it started, or None."""
        now = self._clock()
        settings = await idle_check_settings.settings()
        policy = delivery.load_policy()
        state = IdleState(
            switch=settings[idle_check_settings.KEY], lanes=settings[idle_check_settings.LANES_KEY],
            delivery=settings[idle_check_settings.DELIVERY_KEY],
            attention=settings[idle_check_settings.ATTENTION_KEY], manager_running=self._manager_running(),
            draining=bool(self._drain is not None and self._drain.draining), idle_seconds=None,
            quiet=_quiet(policy, now), dm_cap=policy.dm_daily_cap)
        # The first rows look at the settings and memory only; ``main``'s idleness is asked only after them.
        if (first := plan(state)).action is Action.SKIP and first.skip is not Skip.NOT_IDLE:
            self._skipped(first.skip, now)
            if first.skip is Skip.MANAGER_OFF:  # nothing will take a check that is still waiting for its turn
                await store.settle_running("manager_off", manager_id=MANAGER_ID)
            return None
        state = replace(state, idle_seconds=self._idle_for())
        if (decided := plan(state)).action is Action.SKIP:
            self._skipped(decided.skip, now)
            return None
        async with connection.get_pool().acquire() as conn:
            if not await conn.fetchval("SELECT pg_try_advisory_lock($1::int, $2::int)", *_LOCK):
                return None  # another server's tick is deciding
            try:
                return await self._decide(conn, state, policy, now)
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1::int, $2::int)", *_LOCK)

    async def _decide(self, conn: Any, state: IdleState, policy: delivery.DeliveryPolicy, now: datetime) -> int | None:
        local_date = now.astimezone(policy.timezone).date()
        items: list[dict[str, Any]] = []
        while (decided := plan(state)).action is Action.READ:
            if decided.need is Need.COUNTS:
                last = await store.last_started_at(conn, MANAGER_ID)
                state = replace(state, checks_today=await store.count_on(conn, MANAGER_ID, local_date),
                                 since_last_check=None if last is None else (now - last).total_seconds())
            elif decided.need is Need.TARGET:
                state = replace(state, target=await operator_dm_target())
            elif decided.need is Need.MORNING_BRIEF:
                last = await conn.fetchval(
                    "SELECT max(updated_at) FROM morning_briefs WHERE channel_id = $1 AND chat = $2 "
                    "AND status = 'sent'", state.target.channel_id, state.target.chat)
                state = replace(state, since_morning_brief=None if last is None else (now - last).total_seconds())
            elif decided.need is Need.DM_COUNT:
                state = replace(state, dm_counted=await db_deliveries.counted_on(
                    conn, state.target.channel_id, state.target.chat, local_date))
            else:
                summary = await attention.collect()
                items = select(summary, now=now, mentioned=await store.mentioned_items(
                    conn, MANAGER_ID, now - MENTIONED_WINDOW))
                state = replace(state, new_items=len(items))
        if decided.action is Action.SKIP:
            self._skipped(decided.skip, now)
            return None
        target = state.target
        check_id = await store.insert_running(conn, manager_id=MANAGER_ID, local_date=local_date, items=items,
                                              channel_id=target.channel_id, chat=target.chat)
        try:
            await self._bus.publish(due_envelope(check_id))
        except Exception:
            logger.warning("idle check %s could not be handed to the Manager", check_id, exc_info=True)
            await settle(check_id, "failed", reason="turn_failed")
            return None
        logger.info("idle check %s started with %d item(s)", check_id, len(items))
        return check_id
