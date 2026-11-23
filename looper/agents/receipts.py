"""Action receipts: the line code writes after a Manager or API-client write call (assistant phase 3 RFC, section 6).

``receipt_for`` turns a finished write call (its ``TOOL_ACTIONS`` kind, its arguments and its result or error) into
a typed ``Receipt``; ``render`` writes the line the user sees; ``merge`` keeps a turn's lines honest. ``run_watched``
is the one place ``ManagerOps`` and ``ClientOps`` call while the switch (``receipts_settings``) is on. The wording
is fixed here: a reason comes from a table keyed by ``ErrorCode``, never from an error message, and ``fields`` are
argument names, never values.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import receipts_settings
from .ops import ErrorCode, OpError
from .ops.categories import TOOL_ACTIONS, ActionCategory, ActionKind

logger = logging.getLogger(__name__)

# What the model is told about a call that got a receipt (RFC 6.4); these strings are model-visible.
SHOWN_DONE = "Looper adds a receipt line for this after your reply; do not repeat it."
SHOWN_FAILED = "Looper tells the user after your reply that this did not run; do not say it did."

_NAME_CHARS = 60
LINE_CHARS = 300  # the ``manager_actions.receipt`` limit
_NAME_KEYS = ("name", "title", "display_name", "text")
_ID_KEYS = ("id", "install_id")
_REASONS = {
    ErrorCode.NOT_FOUND: "没找到", ErrorCode.FORBIDDEN: "不允许", ErrorCode.CONFLICT: "状态不对",
    ErrorCode.INVALID: "参数不对", ErrorCode.EXPIRED: "确认已过期", ErrorCode.UNAVAILABLE: "暂时做不了",
    ErrorCode.RATE_LIMITED: "暂时做不了", ErrorCode.INTERNAL: "暂时做不了",
}
_FALLBACK_REASON = "暂时做不了"


class ReceiptOutcome(str, Enum):
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Receipt:
    kind: ActionKind
    outcome: ReceiptOutcome
    subject_name: str | None
    subject_id: str | None
    fields: tuple[str, ...] = ()  # UPDATE: the argument names that were given (never their values)
    error_code: ErrorCode | None = None
    action_id: int | None = None  # the manager_actions row, once it is written


@dataclass
class Watch:
    """What one watched write call came to, for the caller to record and answer with."""

    receipt: Receipt | None = None
    shown: str | None = None  # the ``shown_to_user`` text, when Looper added the line


def kind_of(tool: str, arguments: Mapping[str, Any]) -> ActionKind | None:
    """The ``TOOL_ACTIONS`` entry of a call; None for a read or a tool outside the registry."""
    entry = TOOL_ACTIONS.get(tool)
    if isinstance(entry, Mapping):
        action = arguments.get("action")
        return entry.get(action) if isinstance(action, str) else None
    return entry


def reason(code: ErrorCode) -> str:
    return _REASONS.get(code, _FALLBACK_REASON)


def receipt_for(kind: ActionKind | None, arguments: Mapping[str, Any], outcome: Any) -> Receipt | None:
    """The receipt of a finished write call: ``outcome`` is its result (a mapping) or the ``OpError`` it raised.

    None when the call has no kind, or an error no receipt is written for (a stopped turn, a call that was not this
    turn's, a refusal to run in a background turn, an operator-only refusal, an outbound block): those never ran, or
    the model already tells the user."""
    if kind is None:
        return None
    if isinstance(outcome, OpError):
        if outcome.code not in _REASONS:
            return None
        name, id_ = _subject(kind, arguments, None)
        return Receipt(kind, ReceiptOutcome.FAILED, name, id_, error_code=outcome.code)
    result = outcome if isinstance(outcome, Mapping) else None
    name, id_ = _subject(kind, arguments, result)
    fields: tuple[str, ...] = ()
    if kind.category is ActionCategory.UPDATE:
        fields = _field_names(arguments, {"action", kind.subject})
    return Receipt(kind, ReceiptOutcome.DONE, name, id_, fields)


def render(receipt: Receipt) -> str:
    """The line the user sees (RFC 6.4, in Chinese); at most ``LINE_CHARS`` characters."""
    kind = receipt.kind
    shown = receipt.subject_name or receipt.subject_id
    quoted = f"「{shown}」" if shown else ""
    if receipt.outcome is ReceiptOutcome.FAILED:
        why = reason(receipt.error_code) if receipt.error_code is not None else _FALLBACK_REASON
        line = f"❌ 没做成：{kind.verb}{kind.noun}{quoted}（{why}）"
    elif kind.category is ActionCategory.CREATE:
        extra = f"（{receipt.subject_id}）" if receipt.subject_id and receipt.subject_name else ""
        line = f"✅ 已{kind.verb}{kind.noun}{quoted}{extra}"
    elif kind.category is ActionCategory.UPDATE:
        fields = f"：{'、'.join(receipt.fields)}" if receipt.fields else ""
        line = f"✅ 已{kind.verb}{kind.noun}{quoted}{fields}"
    elif kind.category is ActionCategory.CONTROL:
        line = f"✅ {kind.noun}{quoted}已{kind.verb}"
    elif kind.category is ActionCategory.REMOVE:
        line = f"✅ 已{kind.verb}{kind.noun}{quoted}"
    else:
        line = f"✅ 已把你的话转给{kind.noun}{quoted}"
    return line if len(line) <= LINE_CHARS else line[:LINE_CHARS - 1] + "…"


def merge(receipts: list[Receipt]) -> list[Receipt]:
    """A turn's receipts as the user should see them (RFC 6.4): a failure is dropped when a later call of the same
    action on the same subject succeeded, and identical successes are kept once. Order is kept."""
    kept: list[Receipt] = []
    for index, receipt in enumerate(receipts):
        if receipt.outcome is ReceiptOutcome.FAILED and any(
                later.outcome is ReceiptOutcome.DONE and _same_subject(receipt, later) for later in receipts[index + 1:]):
            continue
        if receipt.outcome is ReceiptOutcome.DONE and receipt in kept:
            continue
        kept.append(receipt)
    return kept


def add_to_turn(turn: Any, receipt: Receipt) -> None:
    """Show ``receipt`` as one of the turn's ``blocks``, after merging with the lines the turn already has."""
    earlier: list[Receipt] = turn.receipts
    merged = merge([*earlier, receipt])
    drop = [item for item in earlier if not any(item is kept for kept in merged)]
    lines = [render(item) for item in drop]
    add = any(receipt is kept for kept in merged)
    for line in lines:  # nothing below raises: the turn changes only once the lines are known
        if line in turn.blocks:
            turn.blocks.remove(line)
    if add:
        turn.blocks.append(render(receipt))
    turn.receipts[:] = merged


async def run_watched(tool: Any, call: Any, watch: Watch) -> Any:
    """``tool.run(call)``, and the call's receipt: when the tool added no block of its own, its line is added to
    ``call.turn.blocks`` and ``watch.shown`` says so. The call's result or error is returned unchanged; a failure
    to build the receipt only logs."""
    turn = call.turn
    before = len(turn.blocks)
    try:
        answer = await tool.run(call)
    except Exception as exc:
        _finish(watch, turn, before, tool.name, call.arguments,
                exc if isinstance(exc, OpError) else OpError(ErrorCode.UNAVAILABLE, "unavailable"))
        raise
    _finish(watch, turn, before, tool.name, call.arguments, answer)
    return answer


def record_refusal(watch: Watch, turn: Any, tool: str, arguments: Mapping[str, Any], error: OpError) -> None:
    """The receipt of a write call refused before it ran (its ``escalate`` raised)."""
    _finish(watch, turn, len(turn.blocks), tool, arguments, error)


async def escalate(tool: Any, call: Any, watch: Watch) -> str | None:
    """``tool.escalate(call)``; with receipts on, a write it refuses still gets its "did not run" line."""
    if tool.escalate is None:
        return None
    try:
        return await tool.escalate(call)
    except OpError as exc:
        if receipts_settings.enabled() and not getattr(exc, "operator_only", False):
            record_refusal(watch, call.turn, tool.name, call.arguments, exc)
        raise


def row_fields(watch: Watch | None) -> dict[str, str]:
    """The ``manager_actions`` columns a watched call fills; none while it has no receipt (off, or a read)."""
    if watch is None or watch.receipt is None:
        return {}
    return {"category": watch.receipt.kind.category.value, "receipt": render(watch.receipt)}


def _finish(watch: Watch, turn: Any, before: int, tool: str, arguments: Mapping[str, Any], outcome: Any) -> None:
    try:
        receipt = receipt_for(kind_of(tool, arguments), arguments, outcome)
        if receipt is None:
            return
        shown = SHOWN_FAILED if receipt.outcome is ReceiptOutcome.FAILED else SHOWN_DONE
        if len(turn.blocks) > before:  # the tool wrote its own line: it stays as it is
            shown = None
        else:
            add_to_turn(turn, receipt)
    except Exception:
        logger.warning("could not build the receipt of a call of %s", tool, exc_info=True)
        return
    watch.receipt, watch.shown = receipt, shown


def _field_names(arguments: Mapping[str, Any], skip: set[str | None]) -> tuple[str, ...]:
    """The fields an update gave: its argument names, an object argument (``fields``, ``values``, a settings group)
    by the names inside it. Never a value."""
    names: list[str] = []
    for key, value in arguments.items():
        if key in skip:
            continue
        for name in (list(value) if isinstance(value, Mapping) and value else [key]):
            if (text := _text(name)) is not None and text not in names:
                names.append(text)
    return tuple(names)


def _same_subject(a: Receipt, b: Receipt) -> bool:
    """The same action on the same subject: by id when both have one, else by name (a call names a schedule, its
    result also gives its id)."""
    if a.kind != b.kind:
        return False
    if a.subject_id and b.subject_id:
        return a.subject_id == b.subject_id
    return a.subject_name == b.subject_name


def _subject(kind: ActionKind, arguments: Mapping[str, Any], result: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """The subject's name and id: from the result, else the arguments, at ``kind.subject``."""
    name: str | None = None
    id_: str | None = None
    if kind.subject is None:
        return None, None
    for source in (result, arguments):
        if source is None:
            continue
        value = source.get(kind.subject)
        found_name, found_id = _from_value(kind.subject, value)
        name, id_ = name or found_name, id_ or found_id
    if name is None and result is not None:
        name = _text(result.get("name"))
        if name is None and isinstance(result.get("item"), Mapping):  # a catalog install names itself in ``item``
            name = _text(result["item"].get("name"))
    return name, id_


def _from_value(key: str, value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, Mapping):
        name = next((text for k in _NAME_KEYS if (text := _text(value.get(k)))), None)
        id_ = next((text for k in (*_ID_KEYS, f"{key}_id") if (text := _text(value.get(k)))), None)
        return name, id_
    text = _text(value)
    if text is None:
        return None, None
    return (None, text) if key.endswith("_id") or key == "gate" else (text, None)


def _text(value: Any) -> str | None:
    """A name or id as one short plain line: whitespace and control characters merged, ``_NAME_CHARS`` at most."""
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    line = re.sub(r"\s+", " ", "".join(ch for ch in str(value) if ch.isprintable() or ch.isspace())).strip()
    if not line:
        return None
    return line if len(line) <= _NAME_CHARS else line[:_NAME_CHARS - 1] + "…"
