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
from . import cron, extraction, subscription_rules, triage_run, triage_settings

# Where lines_v1 writes its time: the same as lark-chat (``zh-CN``, 24-hour, numbers not padded but the clock's).
RENDER = "lines_v1"
RENDER_V2 = "lines_v2"  # lines_v1 with the tag of the rule that took the message: ``【label·area】``
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
    try:
        return subscription_rules.check_header(raw.strip())
    except ValueError as exc:
        raise ValueError(f"subscription.{exc}") from None


def _source(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("subscription.source_id must name an event source")
    return raw.strip()


def _choice(field: str, *allowed: str):
    def parse(raw: Any) -> str:
        if raw not in allowed:
            raise ValueError(f"subscription.{field} must be one of {', '.join(allowed)}")
        return raw
    return parse


def _rules(raw: Any) -> list[dict[str, Any]]:
    try:
        return subscription_rules.parse_rules(raw)
    except ValueError as exc:
        raise ValueError(f"subscription.{exc}") from None


OUTPUT_KINDS = ("push", "leads", "intents", "triage")


def output_kind(sub: Mapping[str, Any] | None) -> str:
    """What a subscription makes of its window: ``push``, or an extraction (``leads``, ``intents``)."""
    return (sub or {}).get("output_kind") or "push"


def is_extraction(sub: Mapping[str, Any] | None) -> bool:
    return output_kind(sub) != "push"


def check_extraction(sub: Mapping[str, Any]) -> None:
    """An extraction's scope is fixed by its kind and its window is its cursor's: rules, a lookback window and a
    daily delivery belong to push subscriptions; the second model's check (``check_items``) belongs to extractions;
    ``ValueError`` saying so."""
    if is_extraction(sub) and (sub.get("rules") or sub.get("window", "cursor") != "cursor"
                               or sub.get("delivery_policy", "per_run") != "per_run"):
        raise ValueError("subscription.rules, window lookback and delivery_policy daily apply to push subscriptions "
                         "only: a leads or intents subscription reads its kind's scope after its cursor")
    if output_kind(sub) == "triage":
        if sub.get("include_chats"):
            raise ValueError("triage has the fixed intents scope; include_chats is not supported")
        if sub.get("check_items"):
            raise ValueError("triage does not use a second model check")
    if not is_extraction(sub) and sub.get("check_items"):
        raise ValueError("subscription.check_items applies to leads and intents subscriptions only: it checks what "
                         "they find before it is stored")


async def check_triage_enabled(sub: Mapping[str, Any], db: Any = None) -> None:
    if output_kind(sub) == "triage" and not await triage_settings.enabled(db):
        raise ValueError(f"cannot create a triage subscription: {triage_settings.TURNED_OFF}")


_PARSERS = {
    "source_id": _source,
    "window": _choice("window", "cursor", "lookback"),
    "delivery_policy": _choice("delivery_policy", "per_run", "daily"),
    "chat_kinds": lambda raw: _enum_list("chat_kinds", raw, db_subs.CHAT_KINDS),
    "sender_kinds": lambda raw: _enum_list("sender_kinds", raw, db_subs.SENDER_KINDS),
    "include_chats": lambda raw: _chat_list("include_chats", raw),
    "exclude_chats": lambda raw: _chat_list("exclude_chats", raw),
    "exclude_mentions_me": lambda raw: _boolean("exclude_mentions_me", raw),
    "include_history": lambda raw: _boolean("include_history", raw),
    "max_age_minutes": lambda raw: _bounded("max_age_minutes", raw, *_MAX_AGE),
    "max_events": lambda raw: _bounded("max_events", raw, *_MAX_EVENTS),
    "text_max_chars": lambda raw: _bounded("text_max_chars", raw, *_TEXT_CHARS),
    "render": _choice("render", RENDER, RENDER_V2),
    "rules": _rules,
    "output_kind": _choice("output_kind", *OUTPUT_KINDS),
    "quiet_word": _quiet_word,
    "output_header": _header,
    "check_items": lambda raw: _boolean("check_items", raw),
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
        # With rules, the filter columns they replace (chat and sender kinds) default.
        needed = [n for n in _REQUIRED_ON_CREATE if not (raw.get("rules") and n in ("chat_kinds", "sender_kinds"))]
        missing = [name for name in needed if name not in raw]
        if missing:
            raise ValueError(f"subscription needs {', '.join(missing)}")
    if update and not raw:
        raise ValueError("give at least one subscription field to change")
    found = {name: _PARSERS[name](value) for name, value in raw.items()}
    if found.get("rules") and not update:
        # The rules select: the filter columns they replace default when the subscription is created (an update that
        # names rules leaves the columns as they are, so removing the rules again gives back what it had).
        found.setdefault("chat_kinds", list(db_subs.CHAT_KINDS))
        found.setdefault("sender_kinds", list(db_subs.SENDER_KINDS))
    return found


def view(sub: Mapping[str, Any]) -> dict[str, Any]:
    """A subscription as a response shows it."""
    return {**{name: list(sub[name]) if isinstance(sub[name], (list, tuple)) else sub[name]
               for name in db_subs.FIELDS},
            "cursor_seq": sub["cursor_seq"], "cursor_at": sub["cursor_at"],
            "target_confirmed_at": sub["target_confirmed_at"]}


# --- What a request does to the confirmation ---


_EXTRACTION_CONFIRMS = ("source_id", "include_chats", "exclude_chats", "check_items")


def _differs(new: Any, old: Any) -> bool:
    return list(new) != list(old or []) if isinstance(new, (list, tuple)) else new != old


def needs_confirmation(row: Mapping[str, Any], changes: Mapping[str, Any], *,
                       stream: Mapping[str, Any] | None = None) -> bool:
    """Whether applying ``changes`` (schedule columns, ``enabled`` and ``notify_*`` as stored) to the subscription
    schedule ``row`` clears its push target's confirmation: enabling it, or a different target, rule or zone while it
    has a target. An extraction (leads, intents) reads the operator's own messages, target or not: enabling it, or a
    different rule, zone, source, chat list or check (``changes`` also carries its subscription fields) clears it."""
    if row.get("kind") == "subscription" and is_extraction(row.get("subscription")):
        sub, asked = row["subscription"], {**changes, **(stream or {})}
        if asked.get("enabled") and not row["enabled"]:
            return True
        return (any(name in asked and asked[name] != row.get(name) for name in ("cron", "timezone"))
                or any(name in asked and _differs(asked[name], sub.get(name)) for name in _EXTRACTION_CONFIRMS))
    if row.get("kind") != "subscription" or not (changes.get("notify_channel", row.get("notify_channel"))):
        return False
    if changes.get("enabled") and not row["enabled"]:
        return True
    current = row.get("subscription") or {}
    if stream and any(name in stream and stream[name] != current.get(name, default)
                      for name, default in (("window", "cursor"), ("delivery_policy", "per_run"))):
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


def render_lines_v2(rows: Sequence[Mapping[str, Any]], *, text_max_chars: int, zone_name: str,
                    rules: Sequence[Mapping[str, Any]]) -> str:
    """``lines_v1`` with the tag of the rule that took each message after the time: ``[time] 【告警·领域】《chat》 ...``."""
    matcher = subscription_rules.Matcher(rules, me=None)
    lines = []
    for row in rows:
        sender = row.get("sender_name") or (row.get("sender_id") or "?")[:10]
        text = _js_slice(_WHITESPACE.sub(" ", row.get("text") or ""), text_max_chars)
        tag = matcher.tag(row) if row.get("rule") is not None else ""
        lines.append(f"[{clock(row['sent_at'], zone_name)}]{' ' + tag if tag else ''}《{row.get('chat_name') or row['chat_id']}》 "
                     f"{sender}: {text}")
    return "\n".join(lines)


def render_lines_v1(rows: Sequence[Mapping[str, Any]], *, text_max_chars: int, zone_name: str) -> str:
    """The window's lines, in the order given (newest first): ``[time]《chat》 sender: text``."""
    lines = []
    for row in rows:
        sender = row.get("sender_name") or (row.get("sender_id") or "?")[:10]
        text = _js_slice(_WHITESPACE.sub(" ", row.get("text") or ""), text_max_chars)
        lines.append(f"[{clock(row['sent_at'], zone_name)}]《{row.get('chat_name') or row['chat_id']}》 "
                     f"{sender}: {text}")
    return "\n".join(lines)


def processor_input(rows: Sequence[Mapping[str, Any]], *, text_max_chars: int, zone_name: str,
                    render: str = RENDER, rules: Sequence[Mapping[str, Any]] = ()) -> str:
    """What the processor is given: the count, then the lines."""
    lines = (render_lines_v2(rows, text_max_chars=text_max_chars, zone_name=zone_name, rules=rules)
             if render == RENDER_V2 else render_lines_v1(rows, text_max_chars=text_max_chars, zone_name=zone_name))
    return f"消息(共 {len(rows)} 条):\n{lines}"


# --- Private content stays with its operator ---


def check_private_target(sub: Mapping[str, Any], channel: str | None, chat: str | None, operator: str | None) -> None:
    """A subscription that can select private content (``subscription_rules.is_private``) may push only to the
    operator's own direct chat: a Lark chat with ``operator``, the user its source is bound to (``user_open_id``). Any
    other chat, a colleague's included, and any source without a bound user, is refused; ``ValueError`` saying why. No
    target is fine: its output stays on the dashboard."""
    if not (channel and chat and subscription_rules.is_private(sub)):
        return
    if channel not in ("lark", "lark_bot") or not operator or chat != operator:
        raise ValueError("this subscription can select private messages (direct chats, messages that @-mention you, "
                         "or a never_quiet list of the messages themselves): it may push only to your own direct chat "
                         "(the user its message source is bound to), not a group or anyone else")


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


async def config_version(schedule: Mapping[str, Any], *, profile: Mapping[str, Any] | None = None,
                         checker: Mapping[str, Any] | None = None) -> str:
    """What a push depends on, as one digest: where it goes, when it fires, the stream it reads and the processor
    (its runtime, provider, model and effort, with the managed hooks that see the messages). A target is confirmed for one
    version; a push goes out only while the schedule still has it. An extraction with ``check_items`` on depends on
    the check's Profile too (``checker``: the one a run launched with; else the role's now), since it reads the
    messages as well."""
    from . import processor
    from .agent_settings import AgentType

    # ``profile``: the facts of the Profile a run actually launched with; else what the schedule would use now.
    profile = dict(profile) if profile is not None else await processor.run_facts(schedule["project_id"], schedule["id"])
    sub = schedule.get("subscription") or {}
    facts = {"channel": schedule.get("notify_channel"), "chat": schedule.get("notify_chat"), "cron": schedule["cron"],
             "timezone": schedule["timezone"], "source": sub.get("source_id"), "processor": profile,
             # Only when true, so a version made before this existed is still the version of a subscription that is not.
             **({"private": True} if subscription_rules.is_private(sub) else {})}
    # Keep existing cursor confirmations byte-identical; opting in pins the new semantics too.
    for name, default in (("window", "cursor"), ("delivery_policy", "per_run")):
        if sub.get(name, default) != default:
            facts[name] = sub[name]
    if is_extraction(sub):  # only an extraction's facts grow: a push subscription stays confirmed under its version
        facts.update(output_kind=output_kind(sub), include_chats=sorted(sub.get("include_chats") or []),
                     exclude_chats=sorted(sub.get("exclude_chats") or []))
        if sub.get("check_items"):  # only when on, so an extraction without it keeps the version it was confirmed under
            facts.update(check_items=True, checker=dict(checker) if checker is not None else
                         await processor.run_facts(None, None, role=AgentType.LEADS_CHECK))
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
             *([f"- {subscription_rules.PRIVATE_NOTE}"] if subscription_rules.is_private(sub) else []),
             f"- 机器人身份：{await bot_identity(channel)}",
             f"- 规则：{describe_zh(rule, zone_name)}；接下来 {len(fires)} 次：{'、'.join(fires)}",
             f"- 每天最多推送：{1 if sub.get('delivery_policy') == 'daily' else fires_per_day(rule, zone_name, now)} 次"
             "（没有新消息或没有内容时不推送）"]
    if sub.get("delivery_policy") == "daily":
        lines.append("- 按触发时刻的本地日期占用名额（手动运行、补跑也计入；"
                     "静默、仅留在面板或已发送/发送状态不确定时保留名额；"
                     "失败或中断且无已发送/发送状态不确定的消息时释放名额）")
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
    "triage": "单聊、@ 你的消息和你自己发的消息，以及勾选的告警机器人；不读导入历史、其他机器人和 Looper 自己的推送",
    "intents": "单聊、@ 你的消息和你自己发的消息，以及勾选的告警机器人（只用于「监控异常排查」）；不读其他机器人和 "
               "Looper 自己的推送",
}


def _usd(value: float) -> str:
    return f"{value:.1f}" if value >= 1 else f"{value:.2f}" if value >= 0.1 else f"{value:.3f}"


async def _extraction_confirmation(schedule: Mapping[str, Any], *, now: datetime, action: str) -> str:
    """``confirmation_text`` of an extraction: what it reads, the chats excluded from extraction, the rule, the
    processor's Profile, the caps and what a day of the last 24 hours' messages would cost (by the unit costs
    ``extraction`` measured)."""
    from ..db.connection import get_pool
    from . import leads_model, processor
    from .agent_settings import AgentType
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
    groups = 0
    if kind == "intents" and await leads_model.group_read_enabled(pool):
        groups = await pool.fetchval("SELECT count(*) FROM intent_group_reads WHERE source_id = $1", sub["source_id"])
    day = sum(row["events"] for row in await db_subs.extraction_tally(
        pool, sub, output_kind=kind, since=now - timedelta(days=1), to_seq=committed, own_apps=await own_bot_app_ids(),
        include_import=False, zone_name=zone_name, group_read=bool(groups)))
    chats = []
    if sub.get("include_chats"):
        chats.append(f"只读其中 {len(sub['include_chats'])} 个指定会话")
    if sub.get("exclude_chats"):
        chats.append(f"另排除 {len(sub['exclude_chats'])} 个会话")
    verb = action.replace("推送目标：", "")  # "确认推送目标：" is a push's word; an extraction confirms itself
    lines = [f"{verb}订阅「{schedule['name']}」：它会用不带工具的处理器整理你的消息"
             f"（{'消息线索' if kind == 'leads' else '事件分流' if kind == 'triage' else '意图识别'}）。",
             f"- 读取范围：{SCOPE_ZH[kind]}{'；' + '，'.join(chats) if chats else ''}；最长回看 "
             f"{sub.get('max_age_minutes', '-')} 分钟，每次最多 {sub.get('max_events', '-')} 条（读不完的下一次接着读）",
             f"- 不整理的会话：{excluded} 个（消息流页设置）",
             f"- 规则：{describe_zh(rule, zone_name)}；接下来 {len(fires)} 次：{'、'.join(fires)}"]
    if groups:
        lines.insert(2, f"- 另读名单上的群：{groups} 个（各自从开始时间起人发的实时消息；每次最多 "
                     f"{extraction.GROUP_READ_RUN_MAX} 条，每个群每天最多 {extraction.GROUP_READ_DAY_MAX} 条）")
    profile = await processor.chosen_profile(schedule["project_id"], schedule["id"])
    named = f"{profile.get('name')}（{profile.get('runtime')} · {profile.get('model') or '默认模型'}）"
    lines.append(f"- 处理 Profile：{named}，来源：{profile.get('source')}（processor.chosen_profile）")
    if profile.get("problem"):
        lines.append(f"- 它现在不能运行：{profile['problem']}")
    if profile.get("managed_hooks"):
        lines.append("- 本机有企业管理的 hooks，会看到处理的消息"
                     f"（{', '.join(profile['managed_hooks'].get('events') or []) or '无法读取'}）")
    if sub.get("check_items"):
        checker = await processor.chosen_profile(role=AgentType.LEADS_CHECK)
        named = f"{checker.get('name')}（{checker.get('runtime')} · {checker.get('model') or '默认模型'}）"
        lines.append("- 第二道检查：入库前，另一个不带工具的模型对照出处复核每一条（保留、丢弃或改置信度），"
                     "它也会读这些消息；花费计入同一次运行的上限")
        lines.append(f"- 复核 Profile：{named}，来源：{checker.get('source')}（Settings → Roles 的 leads_check）")
        if checker.get("problem"):
            lines.append(f"- 复核现在不能运行（条目会未经复核入库）：{checker['problem']}")
        if checker.get("managed_hooks"):
            lines.append("- 本机有企业管理的 hooks，会看到复核的消息"
                         f"（{', '.join(checker['managed_hooks'].get('events') or []) or '无法读取'}）")
    cap, daily = schedule.get("run_budget_usd"), schedule.get("daily_budget_usd")
    lines.append(f"- 花费上限：单次 ${cap:g}；每日 " + (f"${daily:g}" if daily is not None else "未设置"))
    # A firing with something to read starts a run; a day with more than its windows hold fires again at once.
    runs = max(min(fires_per_day(rule, zone_name, now), day), -(-day // max(1, sub.get("max_events") or 1)))
    low, high = extraction.estimate(day, runs)
    lines.append(f"- 预计花费：约 ${_usd(low)}–{_usd(high)} 每天（按最近 24 小时 {day:,} 条、最多 {runs} 次运行估算；"
                 f"单价 {extraction.COST_MEASURED_ON} 实测：每次约 ${extraction.COST_PER_RUN_USD[0]:g}–"
                 f"{extraction.COST_PER_RUN_USD[1]:g}，每条约 ${extraction.COST_PER_EVENT_USD:g}）")
    if kind == "triage":
        lines.append(f"- 分流估算：每次约 ${extraction.COST_PER_RUN_USD[0]:g}–${extraction.COST_PER_RUN_USD[1]:g}，"
                     "另加每条消息费用；沿用抽取实测单价，分流预计高 10%–30%，尚待首次实测")
        lines.append("- 结果去向：仅存储事件和理由，暂不发送；重要事件等待管家交接，摘要事件等待晨报接入")
    if kind == "intents":
        channel, chat = schedule.get("notify_channel"), schedule.get("notify_chat")
        label = next((t["label"] for t in await notify_targets()
                      if (t["channel"], t["chat"]) == (channel, chat)), f"{channel} · {chat}")
        lines.append(f"- 推送目标：{label}（私聊）" if channel else "- 推送目标：未设置（意图识别要一个私聊目标）")
        lines.append(f"- 机器人身份：{await bot_identity(channel)}")
    lines.append("确认后按规则无人值守地整理；改规则、来源、会话范围" + ("、第二道检查" if sub.get("check_items") else "")
                 + "或 Profile 都要再确认。")
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
    "triage": {"kind": "subscription", "name": "事件分流", "cron": "*/10 * * * *",
               "requirement": triage_run.DEFAULT_REQUIREMENT, "run_budget_usd": 0.3, "daily_budget_usd": 3,
               "notify": "failures",
               "subscription": {**_PRESET_STREAM, "sender_kinds": ["user", "app"], "output_kind": "triage",
                                "window": "cursor", "delivery_policy": "per_run"}},
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
        raise ValueError(f"output_kind must be leads, intents or triage, not {kind!r}")
    found = json.loads(json.dumps(PRESETS[kind]))
    found["timezone"] = zone_name
    found["subscription"]["source_id"] = source_id
    return found
