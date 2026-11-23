"""Why a stored message was or was not told to the operator (event-triage RFC, section 7).

Code only: no model call, and the answer is one typed case. The cases are evaluated in the RFC's order.
Logs carry identifiers and the case, never message text, titles, summaries or reason sentences.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..db import message_stream as message_store
from ..db import subscriptions as db_subs
from ..db import triage_events
from . import source_service, triage_settings
from .triage_model import Category, format_event_id

logger = logging.getLogger(__name__)

# Until the Manager handoff exists, an important event has no handling and no delivery decision.
NOT_HANDED = "not handed to the Manager yet (that step is not built)"

_SEQ = re.compile(r"^[0-9]{1,19}$")
_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9]+")
_LINK_KEYS = ("message_id", "openMessageId", "open_message_id")
_BIGINT_MAX = 9223372036854775807
_RULE_ORDER = ("chat_excluded", "imported_history", "own_app", "sender_kind")
_RULE_ZH = {
    "chat_excluded": "会话被排除",
    "imported_history": "导入的历史",
    "own_app": "Looper 自己的应用",
    "sender_kind": "发送人类型不符",
}
_KIND_ZH = {"model": "模型给出", "raised": "代码调高", "uncited": "未引用"}
_CATEGORY_ZH = {"important": "重要", "digest": "进摘要", "ignore": "忽略"}

# Same parameter order as ``_extraction_from``: $1 source, $2–$3 seq range, $4–$5 chats, $6 own apps,
# $7 include import. The in-or-out decision runs ``_extraction_from`` itself. These booleans only name which
# clause failed, in that SQL's order; a mismatch becomes ``unknown`` rather than a guessed rule.
_FACTS_SQL = f"""
    SELECT
      (c.inclusion = 'included' AND c.extraction = 'included'
        AND (cardinality($4::text[]) = 0 OR e.chat_id = ANY($4::text[]))
        AND NOT e.chat_id = ANY($5::text[])) AS chat_ok,
      ($7::boolean OR e.origin <> 'import') AS import_ok,
      (NOT (e.sender_kind = 'app' AND e.sender_id = ANY($6::text[]))) AS own_ok,
      ({db_subs.extraction_scope("triage")}) AS sender_ok
    FROM source_events e
    JOIN event_sources s ON s.id = e.source_id
    JOIN source_chats c ON c.source_id = e.source_id AND c.chat_id = e.chat_id
    WHERE e.source_id = $1 AND e.seq > $2 AND e.seq <= $3
"""


class Case(StrEnum):
    TRIAGE_OFF = "triage_off"
    OUT_OF_SCOPE = "out_of_scope"
    NOT_YET = "not_yet"
    SKIPPED = "skipped"
    EVENT = "event"
    UNCITED = "uncited"
    UNKNOWN = "unknown"


class UnusableRef(ValueError):
    """The reference is not a message id, a message link or a numeric seq."""


class MessageNotFound(LookupError):
    """The reference is well formed and no stored message matches it."""


class AmbiguousMessage(LookupError):
    """The same external id is stored for more than one source."""


@dataclass(frozen=True)
class MessageRef:
    kind: str
    seq: int | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class WhyAnswer:
    case: Case
    cause: str | None = None
    rule: str | None = None
    next_run_at: datetime | None = None
    cursor_seq: int | None = None
    skip_reason: str | None = None
    run_id: str | None = None
    event_id: str | None = None
    category: str | None = None
    reason: str | None = None
    reason_kind: str | None = None
    handling: str | None = None
    checked: tuple[str, ...] = ()
    seq: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The case and its fields. Unrelated nulls are omitted. No message text, title or summary."""
        data: dict[str, Any] = {"case": self.case.value}
        if self.case is Case.TRIAGE_OFF:
            data["cause"] = self.cause
        elif self.case is Case.OUT_OF_SCOPE:
            data["rule"] = self.rule
        elif self.case is Case.NOT_YET:
            data["cursor_seq"] = self.cursor_seq
            data["next_run_at"] = self.next_run_at.isoformat() if self.next_run_at is not None else None
        elif self.case is Case.SKIPPED:
            data["skip_reason"] = self.skip_reason
            data["run_id"] = self.run_id
        elif self.case is Case.EVENT:
            data["event_id"] = self.event_id
            data["category"] = self.category
            data["reason"] = self.reason
            data["reason_kind"] = self.reason_kind
            if self.handling is not None:
                data["handling"] = self.handling
        elif self.case is Case.UNCITED:
            data["run_id"] = self.run_id
        else:
            data["checked"] = list(self.checked)
        return data


def parse_ref(ref: str) -> MessageRef:
    """A Lark message id, a message link, or a numeric seq. No database read."""
    if not isinstance(ref, str):
        raise UnusableRef("message must be a Lark message id, a message link, or a numeric seq")
    text = ref.strip()
    if not text or any(ord(ch) < 32 for ch in text):
        raise UnusableRef("message must be a Lark message id, a message link, or a numeric seq")
    if _SEQ.fullmatch(text):
        number = int(text)
        if number > _BIGINT_MAX:
            raise UnusableRef("message must be a Lark message id, a message link, or a numeric seq")
        return MessageRef("seq", seq=number)
    if text.startswith(("http://", "https://")):
        query = dict(parse_qsl(urlsplit(text).query, keep_blank_values=False))
        for key in _LINK_KEYS:
            found = query.get(key, "").strip()
            if found:
                return MessageRef("external_id", external_id=found)
        token = _MESSAGE_ID.search(text)
        if token is not None:
            return MessageRef("external_id", external_id=token.group(0))
        raise UnusableRef("a message link needs a message id")
    if any(ch.isspace() for ch in text) or len(text) > 200:
        raise UnusableRef("message must be a Lark message id, a message link, or a numeric seq")
    return MessageRef("external_id", external_id=text)


def format_lines(answer: WhyAnswer) -> list[str]:
    """One fact per line, in Chinese. An important event's handling sentence stays the English product line."""
    if answer.case is Case.TRIAGE_OFF:
        why = "开关关闭" if answer.cause == "disabled" else "没有分流订阅"
        return ["分流没开", f"原因：{why}"]
    if answer.case is Case.OUT_OF_SCOPE:
        return ["不在分流范围", f"规则：{_RULE_ZH[answer.rule or '']}"]
    if answer.case is Case.NOT_YET:
        when = answer.next_run_at.isoformat() if answer.next_run_at is not None else "没有下次运行"
        return ["还没轮到这条消息", f"游标：{answer.cursor_seq}", f"下次运行：{when}"]
    if answer.case is Case.SKIPPED:
        return ["这一段被跳过", f"跳过原因：{answer.skip_reason}", f"运行：{answer.run_id}"]
    if answer.case is Case.EVENT:
        lines = [
            f"事件：{answer.event_id}",
            f"类别：{_CATEGORY_ZH[answer.category or '']}",
            f"理由：{answer.reason}",
            f"理由来源：{_KIND_ZH[answer.reason_kind or '']}",
        ]
        if answer.handling is not None:
            lines.append(answer.handling)
        return lines
    if answer.case is Case.UNCITED:
        return ["忽略", "未引用：处理器没有提到这条消息", f"运行：{answer.run_id}"]
    return ["无法判断", "已核对：" + "、".join(answer.checked)]


def _finish(answer: WhyAnswer) -> WhyAnswer:
    # Identifiers and the case only. The reason sentence, title, summary and message text stay out.
    logger.info(
        "triage.why case=%s seq=%s event_id=%s rule=%s skip_reason=%s",
        answer.case.value,
        "-" if answer.seq is None else answer.seq,
        answer.event_id or "-",
        answer.rule or "-",
        answer.skip_reason or "-",
    )
    return answer


def _scope_args(sub: dict[str, Any], seq: int, own_apps: list[str]) -> tuple[Any, ...]:
    # Live triage does not count imported history. Age and the window cap are not scope rules.
    return (sub["source_id"], seq - 1, seq, list(sub["include_chats"]), list(sub["exclude_chats"]),
            own_apps, False)


def _named_rule(row: Any) -> str | None:
    if row is None:
        return None
    flags = {"chat_excluded": row["chat_ok"], "imported_history": row["import_ok"],
             "own_app": row["own_ok"], "sender_kind": row["sender_ok"]}
    for rule in _RULE_ORDER:
        if not flags[rule]:
            return rule
    return None


async def _resolve(conn: Any, parsed: MessageRef) -> dict[str, Any]:
    columns = "seq, source_id, external_id"
    if parsed.kind == "seq":
        row = await conn.fetchrow(f"SELECT {columns} FROM source_events WHERE seq = $1", parsed.seq)
        if row is None:
            raise MessageNotFound("no stored message matches that reference")
        return dict(row)
    where, args = message_store._deletion_where("message", parsed.external_id, None, None, None)
    found = []
    for source in await conn.fetch("SELECT id FROM event_sources ORDER BY id"):
        row = await conn.fetchrow(
            f"SELECT {columns} FROM source_events WHERE source_id = $1 AND ({where})", source["id"], *args)
        if row is not None:
            found.append(dict(row))
    if not found:
        raise MessageNotFound("no stored message matches that reference")
    if len(found) > 1:
        raise AmbiguousMessage("that message id is stored for more than one source")
    return found[0]


async def _subscriptions(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT u.schedule_id, u.source_id, u.cursor_seq, u.include_chats, u.exclude_chats, s.next_fire_at "
        "FROM subscriptions u JOIN schedules s ON s.id = u.schedule_id "
        "WHERE u.output_kind = 'triage' ORDER BY u.schedule_id")
    return [dict(row) for row in rows]


async def _in_scope(conn: Any, sub: dict[str, Any], seq: int, own_apps: list[str]) -> bool:
    row = await conn.fetchrow("SELECT e.seq " + db_subs._extraction_from("triage"),
                              *_scope_args(sub, seq, own_apps))
    return row is not None


async def _covering_runs(conn: Any, seq: int, schedule_ids: set[str]) -> list[dict[str, Any]]:
    runs = await db_subs.runs_for_message(seq, db=conn)
    if not runs:
        return []
    rows = await conn.fetch("SELECT id, schedule_id FROM schedule_runs WHERE id = ANY($1::text[])",
                            [run["run_id"] for run in runs])
    allowed = {row["id"] for row in rows if row["schedule_id"] in schedule_ids}
    return [run for run in runs if run["run_id"] in allowed]


async def why(conn: Any, ref: str, *, now: datetime) -> WhyAnswer:
    """The section 7 answer for ``ref``. ``now`` is the caller's clock and must be timezone-aware."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    parsed = parse_ref(ref)
    if not await triage_settings.enabled(db=conn):
        return _finish(WhyAnswer(case=Case.TRIAGE_OFF, cause="disabled"))
    subs = await _subscriptions(conn)
    if not subs:
        return _finish(WhyAnswer(case=Case.TRIAGE_OFF, cause="no_subscription"))
    message = await _resolve(conn, parsed)
    seq = int(message["seq"])
    on_source = [sub for sub in subs if sub["source_id"] == message["source_id"]]
    if not on_source:
        return _finish(WhyAnswer(case=Case.UNKNOWN, checked=("no_triage_subscription_for_source",), seq=seq))
    own_apps = sorted(await source_service.own_bot_app_ids())
    scoped = []
    rules: list[str | None] = []
    for sub in on_source:
        if await _in_scope(conn, sub, seq, own_apps):
            scoped.append(sub)
            continue
        facts = await conn.fetchrow(_FACTS_SQL, *_scope_args(sub, seq, own_apps))
        if facts is None:
            rules.append("message_has_no_chat_row")
        else:
            rules.append(_named_rule(facts) or "scope_sql_disagrees")
    if not scoped:
        named = {rule for rule in rules if rule in _RULE_ORDER}
        if len(named) == 1 and named == set(rules):
            return _finish(WhyAnswer(case=Case.OUT_OF_SCOPE, rule=next(iter(named)), seq=seq))
        checked = tuple(rule if rule not in _RULE_ORDER else f"rule:{rule}" for rule in rules)
        return _finish(WhyAnswer(case=Case.UNKNOWN, checked=checked or ("scope_sql_disagrees",), seq=seq))
    if all(seq > int(sub["cursor_seq"]) for sub in scoped):
        times = [sub["next_fire_at"] for sub in scoped if sub["next_fire_at"] is not None]
        return _finish(WhyAnswer(
            case=Case.NOT_YET, cursor_seq=max(int(sub["cursor_seq"]) for sub in scoped),
            next_run_at=min(times) if times else None, seq=seq))
    runs = await _covering_runs(conn, seq, {sub["schedule_id"] for sub in subs})
    events = await triage_events.for_message(seq, db=conn)
    open_runs = [run for run in runs if run.get("outcome") != "skipped"]
    if runs and not open_runs:
        skipped = runs[-1]
        return _finish(WhyAnswer(case=Case.SKIPPED, skip_reason=skipped.get("skip_reason"),
                                 run_id=skipped["run_id"], seq=seq))
    if events:
        event = events[0]
        handling = NOT_HANDED if event.category is Category.IMPORTANT else None
        return _finish(WhyAnswer(
            case=Case.EVENT, event_id=format_event_id(event.id), category=event.category.value,
            reason=event.reason, reason_kind=event.reason_kind.value, handling=handling,
            run_id=event.run_id, seq=seq))
    if open_runs:
        return _finish(WhyAnswer(case=Case.UNCITED, run_id=open_runs[-1]["run_id"], seq=seq))
    checked = ("in_scope", "cursor_seq=" + ",".join(str(int(sub["cursor_seq"])) for sub in scoped),
               f"covering_runs={len(runs)}", f"events={len(events)}")
    return _finish(WhyAnswer(case=Case.UNKNOWN, checked=checked, seq=seq))
