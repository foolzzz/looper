"""Subscriptions: the stream half of a ``kind = 'subscription'`` schedule (message-stream RFC, section 7).

A subscription turns the events its source ingested after its cursor into one tool-less processor run
(``processor.run``) and, once its push target is confirmed, into a message. This module holds what is pure or
validates: the fields of a request, the selection a window applies, the ``lines_v1`` text the processor reads, what
is made of its reply, the rule in words, and the text the operator confirms a push target with. The runs are in
``subscription_runs`` (``SubscriptionRuns``), fired by the scheduler. A subscription whose ``output_kind`` is
``leads`` or ``intents`` stores what it finds instead of pushing (``extraction``); its presets are ``PRESETS``.

``lines_v1`` is the line format of lark-chat's hourly digest, kept exactly: ``[time]《chat》 sender: text`` with
the text's whitespace collapsed and cut to ``text_max_chars`` UTF-16 units, newest message first, under a line that
counts them, so a subscription replaces that collector without the processor reading anything different.

A push target needs the operator's word: enabling the subscription, changing its target, its rule or its zone
clears ``target_confirmed_at`` and, until the operator confirms (``schedule_admin.update`` or ``confirm_target``,
through the confirmation every write of a schedule goes through), the output stays on the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..db import subscriptions as db_subs
from . import cron, extraction

# Where lines_v1 writes its time: the same as lark-chat (``zh-CN``, 24-hour, numbers not padded but the clock's).
RENDER = "lines_v1"
MAX_CHATS = 100
CHAT_ID_MAX_CHARS = 200
HEADER_MAX_CHARS = 200
# A push is sent in messages of at most PUSH_PART_CHARS; a reply that needs more than PUSH_MAX_PARTS is cut.
PUSH_PART_CHARS = 4000
PUSH_MAX_PARTS = 3
CUT_NOTE = "（输出过长，已截断）"
# What a processor run costs at most when its schedule sets no cap: none; a subscription must set one.
NEXT_FIRES_SHOWN = 3
DAYS_COUNTED = 14

_REQUIRED_ON_CREATE = ("source_id", "chat_kinds", "sender_kinds", "max_age_minutes", "max_events", "text_max_chars")
_MAX_AGE = (5, 10080)
_MAX_EVENTS = (1, 2000)
_TEXT_CHARS = (20, 4000)
_QUIET = re.compile(r"\S{1,40}")


def _enum_list(field: str, raw: Any, allowed: Sequence[str]) -> list[str]:
    if (not isinstance(raw, list) or not raw or any(not isinstance(item, str) for item in raw)
            or any(item not in allowed for item in raw)):
        raise ValueError(f"subscription.{field} must be a nonempty list of {', '.join(allowed)}")
    return list(dict.fromkeys(raw))


def _chat_list(field: str, raw: Any) -> list[str]:
    if (not isinstance(raw, list) or len(raw) > MAX_CHATS
            or any(not isinstance(item, str) or not item.strip() or len(item) > CHAT_ID_MAX_CHARS for item in raw)):
        raise ValueError(f"subscription.{field} must be a list of at most {MAX_CHATS} chat ids "
                         f"(each 1 to {CHAT_ID_MAX_CHARS} characters)")
    return list(dict.fromkeys(item.strip() for item in raw))


def _bounded(field: str, raw: Any, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not low <= raw <= high:
        raise ValueError(f"subscription.{field} must be a whole number from {low} to {high}")
    return raw


def _boolean(field: str, raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise ValueError(f"subscription.{field} must be true or false")
    return raw


def _quiet_word(raw: Any) -> str:
    if not isinstance(raw, str) or not _QUIET.fullmatch(raw):
        raise ValueError("subscription.quiet_word must be one word of 1 to 40 characters without spaces")
    return raw


def _header(raw: Any) -> str:
    if not isinstance(raw, str) or len(raw.strip()) > HEADER_MAX_CHARS or "\x00" in raw:
        raise ValueError(f"subscription.output_header must be text of at most {HEADER_MAX_CHARS} characters")
    return raw.strip()


def _source(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("subscription.source_id must name an event source")
    return raw.strip()


def _choice(field: str, allowed: str):
    def parse(raw: Any) -> str:
        if raw != allowed:
            raise ValueError(f"subscription.{field} must be {allowed!r}")
        return raw
    return parse


OUTPUT_KINDS = ("push", "leads", "intents")


def _output_kind(raw: Any) -> str:
    if raw not in OUTPUT_KINDS:
        raise ValueError(f"subscription.output_kind must be one of {', '.join(OUTPUT_KINDS)}")
    return raw


def output_kind(sub: Mapping[str, Any] | None) -> str:
    """What a subscription makes of its window: ``push``, or an extraction (``leads``, ``intents``)."""
    return (sub or {}).get("output_kind") or "push"


def is_extraction(sub: Mapping[str, Any] | None) -> bool:
    return output_kind(sub) != "push"


_PARSERS = {
    "source_id": _source,
    "chat_kinds": lambda raw: _enum_list("chat_kinds", raw, db_subs.CHAT_KINDS),
    "sender_kinds": lambda raw: _enum_list("sender_kinds", raw, db_subs.SENDER_KINDS),
    "include_chats": lambda raw: _chat_list("include_chats", raw),
    "exclude_chats": lambda raw: _chat_list("exclude_chats", raw),
    "exclude_mentions_me": lambda raw: _boolean("exclude_mentions_me", raw),
    "include_history": lambda raw: _boolean("include_history", raw),
    "max_age_minutes": lambda raw: _bounded("max_age_minutes", raw, *_MAX_AGE),
    "max_events": lambda raw: _bounded("max_events", raw, *_MAX_EVENTS),
    "text_max_chars": lambda raw: _bounded("text_max_chars", raw, *_TEXT_CHARS),
    "render": _choice("render", RENDER),
    "output_kind": _output_kind,
    "quiet_word": _quiet_word,
    "output_header": _header,
}
# Only a subscription's own state moves these: a request cannot set them.
_STATE_FIELDS = ("cursor_seq", "cursor_at", "target_confirmed_at")


def parse(raw: Any, *, update: bool) -> dict[str, Any]:
    """The subscription fields of a request, validated; ``ValueError`` saying which one is wrong. A new
    subscription names its source, its chat and sender kinds, and its window's age, size and text length."""
    if not isinstance(raw, Mapping):
        raise ValueError("subscription must be an object: the stream the schedule reads")
    unknown = [key for key in raw if key not in _PARSERS]
    if unknown:
        state = " (its cursor and confirmation are set by the operations that reset or confirm them)" \
            if unknown[0] in _STATE_FIELDS else ""
        raise ValueError(f"unknown subscription field {unknown[0]!r}{state}; a subscription has {', '.join(_PARSERS)}")
    if not update:
        missing = [name for name in _REQUIRED_ON_CREATE if name not in raw]
        if missing:
            raise ValueError(f"subscription needs {', '.join(missing)}")
    if update and not raw:
        raise ValueError("give at least one subscription field to change")
    return {name: _PARSERS[name](value) for name, value in raw.items()}


def view(sub: Mapping[str, Any]) -> dict[str, Any]:
    """A subscription as a response shows it."""
    return {**{name: list(sub[name]) if isinstance(sub[name], (list, tuple)) else sub[name]
               for name in db_subs.FIELDS},
            "cursor_seq": sub["cursor_seq"], "cursor_at": sub["cursor_at"],
            "target_confirmed_at": sub["target_confirmed_at"]}


# --- What a request does to the confirmation ---


_EXTRACTION_CONFIRMS = ("source_id", "include_chats", "exclude_chats")


def _differs(new: Any, old: Any) -> bool:
    return list(new) != list(old or []) if isinstance(new, (list, tuple)) else new != old


def needs_confirmation(row: Mapping[str, Any], changes: Mapping[str, Any]) -> bool:
    """Whether applying ``changes`` (schedule columns, ``enabled`` and ``notify_*`` as stored) to the subscription
    schedule ``row`` clears its push target's confirmation: enabling it, or a different target, rule or zone while it
    has a target. An extraction (leads, intents) reads the operator's own messages, target or not: enabling it, or a
    different rule, zone, source or chat list (``changes`` also carries its subscription fields) clears it."""
    if row.get("kind") == "subscription" and is_extraction(row.get("subscription")):
        sub = row["subscription"]
        if changes.get("enabled") and not row["enabled"]:
            return True
        return (any(name in changes and changes[name] != row.get(name) for name in ("cron", "timezone"))
                or any(name in changes and _differs(changes[name], sub.get(name)) for name in _EXTRACTION_CONFIRMS))
    if row.get("kind") != "subscription" or not (changes.get("notify_channel", row.get("notify_channel"))):
        return False
    if changes.get("enabled") and not row["enabled"]:
        return True
    return any(name in changes and changes[name] != row.get(name)
               for name in ("notify_channel", "notify_chat", "cron", "timezone"))


# --- The rule in words ---

_WEEKDAYS = "日一二三四五六"
_ALL_DAYS = frozenset(range(1, 32))
_ALL_WEEKDAYS = frozenset(range(7))
_ALL_MONTHS = frozenset(range(1, 13))


def describe_zh(rule: cron.CronRule, zone_name: str) -> str:
    """The rule in Chinese ("每小时第 5 分钟（America/Los_Angeles）"); the raw rule when no wording fits."""
    raw = f"按 cron 规则 {rule.text}（{zone_name}）"
    if rule.months != _ALL_MONTHS or not (rule.any_day or rule.any_weekday):
        return raw
    if rule.days == _ALL_DAYS and rule.weekdays == _ALL_WEEKDAYS:
        days = ""
    elif rule.days == _ALL_DAYS and rule.weekdays == frozenset(range(1, 6)):
        days = "每个工作日"
    elif rule.days == _ALL_DAYS:
        days = "每周" + "、".join(_WEEKDAYS[day] for day in sorted(rule.weekdays))
    else:
        return raw
    minutes, hours = rule.minutes, rule.hours
    if len(hours) == 24:
        step = minutes[1] - minutes[0] if len(minutes) > 1 else None
        if len(minutes) == 1:
            when = f"每小时第 {minutes[0]} 分钟"
        elif step is not None and minutes[0] == 0 and minutes == tuple(range(0, 60, step)):
            when = f"每 {step} 分钟"
        else:
            when = "每小时第 " + "、".join(str(m) for m in minutes) + " 分钟"
        return f"{days}{when}（{zone_name}）"
    times = "、".join(f"{hour:02d}:{minute:02d}" for hour in hours for minute in minutes)
    return f"{days or '每天'} {times}（{zone_name}）"


def fires_per_day(rule: cron.CronRule, zone_name: str, now: datetime) -> int:
    """The most times the rule fires in one calendar day (in its zone) over the next two weeks."""
    tz: ZoneInfo = cron.zone(zone_name)
    counts: dict[Any, int] = {}
    for moment in cron.fires_between(rule, tz, now, now + timedelta(days=DAYS_COUNTED), limit=20_000):
        day = moment.astimezone(tz).date()
        counts[day] = counts.get(day, 0) + 1
    return max(counts.values(), default=0)


# --- What the processor reads ---


def _js_slice(text: str, limit: int) -> str:
    """The first ``limit`` UTF-16 code units of ``text``, as JavaScript's ``slice`` takes them, without a cut
    surrogate pair (which could not be stored)."""
    if len(text) <= limit and text.isascii():
        return text
    units = text.encode("utf-16-le", "surrogatepass")
    if len(units) <= limit * 2:
        return text
    cut = units[:limit * 2]
    cut = cut[:-2] if 0xD800 <= int.from_bytes(cut[-2:], "little") <= 0xDBFF else cut
    return cut.decode("utf-16-le", "surrogatepass")


_WHITESPACE = re.compile(r"[\s﻿]+")


def clock(moment: datetime, zone_name: str) -> str:
    """A time as lark-chat writes it: ``2026/11/19 8:05:03`` is ``2026/11/19 08:05:03``, the month and day not padded."""
    local = moment.astimezone(cron.zone(zone_name))
    return f"{local.year}/{local.month}/{local.day} {local:%H:%M:%S}"


def render_lines_v1(rows: Sequence[Mapping[str, Any]], *, text_max_chars: int, zone_name: str) -> str:
    """The window's lines, in the order given (newest first): ``[time]《chat》 sender: text``."""
    lines = []
    for row in rows:
        sender = row.get("sender_name") or (row.get("sender_id") or "?")[:10]
        text = _js_slice(_WHITESPACE.sub(" ", row.get("text") or ""), text_max_chars)
        lines.append(f"[{clock(row['sent_at'], zone_name)}]《{row.get('chat_name') or row['chat_id']}》 "
                     f"{sender}: {text}")
    return "\n".join(lines)


def processor_input(rows: Sequence[Mapping[str, Any]], *, text_max_chars: int, zone_name: str) -> str:
    """What the processor is given: the count, then the lines."""
    return f"消息(共 {len(rows)} 条):\n{render_lines_v1(rows, text_max_chars=text_max_chars, zone_name=zone_name)}"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- What is made of its reply ---

_QUIET_STRIP = re.compile(r"[\s*#>`._-]")


def is_quiet(reply: str, quiet_word: str) -> bool:
    """Whether ``reply`` says there is nothing to push: empty, or the quiet word whatever its case once whitespace
    and Markdown marks (``* # > ` . _ -``) are removed, as lark-chat's digest reads it."""
    stripped = _QUIET_STRIP.sub("", reply)
    return not reply.strip() or stripped.casefold() == _QUIET_STRIP.sub("", quiet_word).casefold()


_AT_TAG = re.compile(r"<at\b[^>]*>(.*?)</at>", re.IGNORECASE | re.DOTALL)
_AT_LOOSE = re.compile(r"</?at\b[^>]*>", re.IGNORECASE)
_AT_KEY = re.compile(r"@_user_\d+")
_AT_ALL = re.compile(r"[@＠](?=(?:所有人|all\b|everyone\b|here\b))", re.IGNORECASE)
_AT_OPEN_ID = re.compile(r"[@＠](?=(?:ou|on|oc)_[A-Za-z0-9]+)")


def strip_mentions(text: str) -> str:
    """The text without anything that would @ somebody in Lark: ``<at ...>`` markup (its inner name kept), ``@_user_N``
    keys, ``@所有人`` / ``@all`` and ``@ou_...``; a name that is only written stays."""
    text = _AT_TAG.sub(lambda found: found.group(1), text)
    text = _AT_LOOSE.sub("", text)
    text = _AT_KEY.sub("", text)
    return _AT_OPEN_ID.sub("", _AT_ALL.sub("", text))


def push_text(reply: str, header: str) -> str:
    """The message a reply makes: the header, a blank line, the reply without @ marks."""
    body = strip_mentions(reply).strip()
    if not body:
        return ""
    return f"{header}\n\n{body}" if header else body


def push_parts(text: str) -> list[str]:
    """``text`` as the messages it is sent in (``task_notice.split_message``), at most ``PUSH_MAX_PARTS``."""
    from .task_notice import split_message

    parts = split_message(text, PUSH_PART_CHARS)
    if len(parts) > PUSH_MAX_PARTS:
        parts = parts[:PUSH_MAX_PARTS]
        parts[-1] = parts[-1][:PUSH_PART_CHARS - len(CUT_NOTE) - 1] + "\n" + CUT_NOTE
    return parts


def idempotency_keys(run_id: str, count: int) -> list[str]:
    """The key of each message of a run's push: the run's id, and its id and the part's number for a later part
    (Lark takes at most 50 characters)."""
    return [run_id, *(f"{run_id}-{number}" for number in range(2, count + 1))][:count]


# --- What the operator confirms ---


async def config_version(schedule: Mapping[str, Any], *, profile: Mapping[str, Any] | None = None) -> str:
    """What a push depends on, as one digest: where it goes, when it fires, the stream it reads and the processor
    (its runtime, provider, model and effort, with the managed hooks that see the messages). A target is confirmed for one
    version; a push goes out only while the schedule still has it."""
    from . import processor

    # ``profile``: the facts of the Profile a run actually launched with; else what the schedule would use now.
    profile = dict(profile) if profile is not None else await processor.run_facts(schedule["project_id"], schedule["id"])
    sub = schedule.get("subscription") or {}
    facts = {"channel": schedule.get("notify_channel"), "chat": schedule.get("notify_chat"), "cron": schedule["cron"],
             "timezone": schedule["timezone"], "source": sub.get("source_id"), "processor": profile}
    if is_extraction(sub):  # only an extraction's facts grow: a push subscription stays confirmed under its version
        facts.update(output_kind=output_kind(sub), include_chats=sorted(sub.get("include_chats") or []),
                     exclude_chats=sorted(sub.get("exclude_chats") or []))
    return digest(json.dumps(facts, sort_keys=True, ensure_ascii=False))


async def confirmation_text(schedule: Mapping[str, Any], *, now: datetime, action: str) -> str:
    """The text the operator is shown before a subscription's push target is confirmed: ``schedule`` as it will be
    once ``action`` is done. Lists where it pushes and as which bot, the rule in words with its next fires, how often
    at most, the processor's Profile and where that choice comes from, whether the machine's managed hooks see the
    messages, and the caps."""
    from . import processor
    from .schedules import notify_targets

    sub = schedule.get("subscription") or {}
    if is_extraction(sub):
        return await _extraction_confirmation(schedule, now=now, action=action)
    channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
    rule, zone_name = cron.parse(schedule["cron"]), schedule["timezone"]
    tz = cron.zone(zone_name)
    fires = [moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") for moment in
             cron.next_fires(rule, tz, now, NEXT_FIRES_SHOWN)]
    label = next((t["label"] for t in await notify_targets()
                  if (t["channel"], t["chat"]) == (channel, chat)), f"{channel} · {chat}")
    kind = "群聊" if (chat or "").startswith(("oc_", "-")) else "私聊"
    lines = [f"{action}订阅「{schedule['name']}」：它会把处理结果推送出去，目标需要你确认。",
             f"- 推送目标：{label}（{kind}）",
             f"- 机器人身份：{await bot_identity(channel)}",
             f"- 规则：{describe_zh(rule, zone_name)}；接下来 {len(fires)} 次：{'、'.join(fires)}",
             f"- 每天最多推送：{fires_per_day(rule, zone_name, now)} 次（没有新消息或没有内容时不推送）"]
    profile = await processor.chosen_profile(schedule["project_id"], schedule["id"])
    named = f"{profile.get('name')}（{profile.get('runtime')} · {profile.get('model') or '默认模型'}）"
    lines.append(f"- 处理 Profile：{named}，来源：{profile.get('source')}（processor.chosen_profile）")
    if profile.get("problem"):
        lines.append(f"- 它现在不能运行：{profile['problem']}")
    if profile.get("managed_hooks"):
        lines.append("- 本机有企业管理的 hooks，会看到处理的消息"
                     f"（{', '.join(profile['managed_hooks'].get('events') or []) or '无法读取'}）")
    cap = schedule.get("run_budget_usd")
    daily = schedule.get("daily_budget_usd")
    lines.append(f"- 花费上限：单次 ${cap:g}；每日 " + (f"${daily:g}" if daily is not None else "未设置"))
    lines.append(f"- 读取：来源 {sub.get('source_id', '-')}，窗口最长回看 {sub.get('max_age_minutes', '-')} 分钟，"
                 f"最多 {sub.get('max_events', '-')} 条")
    lines.append("确认后，启用的订阅按规则无人值守地推送；改目标、规则、时区或重新启用都要再确认。")
    return "\n".join(lines)


# What the confirmation says an extraction reads (message-leads-intents RFC, decision 5 and R-1).
SCOPE_ZH = {
    "leads": "群和话题群里人发的消息、别人发给你的单聊和你自己发的消息；不读机器人和 Looper 自己的推送",
    "intents": "单聊、@ 你的消息和你自己发的消息，以及勾选的告警机器人（只用于「监控异常排查」）；不读其他机器人和 "
               "Looper 自己的推送",
}


def _usd(value: float) -> str:
    return f"{value:.1f}" if value >= 1 else f"{value:.2f}"


async def _extraction_confirmation(schedule: Mapping[str, Any], *, now: datetime, action: str) -> str:
    """``confirmation_text`` of an extraction: what it reads, the chats excluded from extraction, the rule, the
    processor's Profile, the caps and what a day of the last 24 hours' messages would cost (inferred)."""
    from ..db.connection import get_pool
    from . import processor
    from .schedules import notify_targets
    from .source_service import own_bot_app_ids

    sub = schedule.get("subscription") or {}
    kind = output_kind(sub)
    rule, zone_name = cron.parse(schedule["cron"]), schedule["timezone"]
    tz = cron.zone(zone_name)
    fires = [moment.astimezone(tz).strftime("%Y-%m-%d %H:%M") for moment in
             cron.next_fires(rule, tz, now, NEXT_FIRES_SHOWN)]
    pool = get_pool()
    excluded = await pool.fetchval("SELECT count(*) FROM source_chats WHERE source_id = $1 AND extraction = 'excluded'",
                                   sub.get("source_id"))
    committed = await db_subs.committed_seq(pool, sub.get("source_id")) or 0
    day = sum(row["events"] for row in await db_subs.extraction_tally(
        pool, sub, output_kind=kind, since=now - timedelta(days=1), to_seq=committed, own_apps=await own_bot_app_ids(),
        include_import=False, zone_name=zone_name))
    chats = []
    if sub.get("include_chats"):
        chats.append(f"只读其中 {len(sub['include_chats'])} 个指定会话")
    if sub.get("exclude_chats"):
        chats.append(f"另排除 {len(sub['exclude_chats'])} 个会话")
    verb = action.replace("推送目标：", "")  # "确认推送目标：" is a push's word; an extraction confirms itself
    lines = [f"{verb}订阅「{schedule['name']}」：它会用不带工具的处理器整理你的消息"
             f"（{'消息线索' if kind == 'leads' else '意图识别'}）。",
             f"- 读取范围：{SCOPE_ZH[kind]}{'；' + '，'.join(chats) if chats else ''}；最长回看 "
             f"{sub.get('max_age_minutes', '-')} 分钟，每次最多 {sub.get('max_events', '-')} 条（读不完的下一次接着读）",
             f"- 不整理的会话：{excluded} 个（消息流页设置）",
             f"- 规则：{describe_zh(rule, zone_name)}；接下来 {len(fires)} 次：{'、'.join(fires)}"]
    profile = await processor.chosen_profile(schedule["project_id"], schedule["id"])
    named = f"{profile.get('name')}（{profile.get('runtime')} · {profile.get('model') or '默认模型'}）"
    lines.append(f"- 处理 Profile：{named}，来源：{profile.get('source')}（processor.chosen_profile）")
    if profile.get("problem"):
        lines.append(f"- 它现在不能运行：{profile['problem']}")
    if profile.get("managed_hooks"):
        lines.append("- 本机有企业管理的 hooks，会看到处理的消息"
                     f"（{', '.join(profile['managed_hooks'].get('events') or []) or '无法读取'}）")
    cap, daily = schedule.get("run_budget_usd"), schedule.get("daily_budget_usd")
    lines.append(f"- 花费上限：单次 ${cap:g}；每日 " + (f"${daily:g}" if daily is not None else "未设置"))
    low, high = (day * rate for rate in extraction.COST_PER_EVENT_USD)
    lines.append(f"- 预计花费：约 ${_usd(low)}–{_usd(high)} 每天（按最近 24 小时 {day:,} 条估算，推测）")
    if kind == "intents":
        channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
        label = next((t["label"] for t in await notify_targets()
                      if (t["channel"], t["chat"]) == (channel, chat)), f"{channel} · {chat}")
        lines.append(f"- 推送目标：{label}（私聊）" if channel else "- 推送目标：未设置（意图识别要一个私聊目标）")
        lines.append(f"- 机器人身份：{await bot_identity(channel)}")
    lines.append("确认后按规则无人值守地整理；改规则、来源、会话范围或 Profile 都要再确认。")
    return "\n".join(lines)


async def bot_identity(channel: str | None) -> str:
    """The bot a push goes out as: its name and the channel it is."""
    from ..db import channel_configs as db_channel_configs
    from .message import builtin_channel_id

    if not channel:
        return "-"
    channel_id = builtin_channel_id(channel)
    try:
        config = await db_channel_configs.get_config(channel_id) or {}
        settings = config.get("config") or {}
        if channel == "lark":
            from .channels import lark_cli

            found = await lark_cli.identity(settings.get("profile") or None)
            return f"{found.bot_name or '飞书机器人'}（{channel_id}）"
        name = settings.get("bot_name") or settings.get("name")
        return f"{name}（{channel_id}）" if name else channel_id
    except Exception:
        return f"{channel_id}（读不到机器人名称）"


# --- Presets: the extraction subscriptions as the dashboard creates them ---

_PRESET_STREAM = {"chat_kinds": ["p2p", "group", "topic"], "include_history": True, "max_age_minutes": 10080,
                  "max_events": 500, "text_max_chars": 500, "quiet_word": "NONE", "output_header": ""}
PRESETS: dict[str, dict[str, Any]] = {
    "leads": {"kind": "subscription", "name": "消息线索", "cron": "7 * * * *",
              "requirement": extraction.LEADS_INSTRUCTIONS, "run_budget_usd": 0.6, "daily_budget_usd": 10,
              "notify": "failures",
              "subscription": {**_PRESET_STREAM, "sender_kinds": ["user"], "output_kind": "leads"}},
    "intents": {"kind": "subscription", "name": "意图识别", "cron": "*/10 * * * *",
                "requirement": extraction.INTENTS_INSTRUCTIONS, "run_budget_usd": 0.3, "daily_budget_usd": 3,
                "notify": "failures",
                "subscription": {**_PRESET_STREAM, "sender_kinds": ["user", "app"], "output_kind": "intents"}},
}


def preset(kind: str, *, source_id: str, zone_name: str) -> dict[str, Any]:
    """The ``fields`` of ``schedule_admin.create`` for an extraction subscription, all but ``project_id`` and, for
    intents, ``notify_target``: ``PRESETS`` with the source and the server's zone. Its chat kinds, sender kinds and
    the rest of the stream filter only satisfy validation: an extraction's scope is fixed by its kind."""
    if kind not in PRESETS:
        raise ValueError(f"output_kind must be leads or intents, not {kind!r}")
    found = json.loads(json.dumps(PRESETS[kind]))
    found["timezone"] = zone_name
    found["subscription"]["source_id"] = source_id
    return found
