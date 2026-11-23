"""Operator-only lookup: why one stored message was or was not reported. No Manager tool."""

from datetime import datetime, timezone

from ...db.connection import get_pool
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT
from ..ops import ErrorCode, OpError
from ..triage_why import AmbiguousMessage, MessageNotFound, UnusableRef, why

SUMMARY = "Say why a stored message was or was not reported."
_CASE = {"type": "string", "enum": [
    "triage_off", "out_of_scope", "not_yet", "skipped", "event", "uncited", "unknown"]}
WHY = _object({
    "case": _CASE,
    "cause": {"type": "string", "enum": ["disabled", "no_subscription"]},
    "rule": {"type": "string", "enum": ["chat_excluded", "imported_history", "own_app", "sender_kind"]},
    "next_run_at": _NULL_STR,
    "cursor_seq": _INT,
    "skip_reason": _STR,
    "run_id": _STR,
    "event_id": _STR,
    "category": {"type": "string", "enum": ["important", "digest", "ignore"]},
    "reason": _STR,
    "reason_kind": {"type": "string", "enum": ["model", "uncited", "raised"]},
    "handling": _STR,
    "checked": _array(_STR),
}, ["case"])

RESULTS = {}
# Inside Looper: a local read of stored rows. No model call and no delivery, so no Manager tool in v1.
OPERATIONS = (
    Operation("triage.why", "POST", "/api/v1/triage/why", SUMMARY,
              SUMMARY + " A Lark message id, a message link or a numeric seq. One typed case; no model is called.",
              _object({"message": _STR}, closed=True), WHY,
              risk="read", operator_only=True, level=ActionLevel.INSIDE),
)


async def run(server, operation, arguments, principal, headers):
    message = arguments.get("message")
    if not isinstance(message, str) or not message.strip():
        raise OpError(ErrorCode.INVALID, "message must be a Lark message id, a message link, or a numeric seq")
    try:
        answer = await why(get_pool(), message, now=datetime.now(timezone.utc))
    except UnusableRef as error:
        raise OpError(ErrorCode.INVALID, "message must be a Lark message id, a message link, or a numeric seq") from error
    except MessageNotFound as error:
        raise OpError(ErrorCode.NOT_FOUND, "no stored message matches that reference") from error
    except AmbiguousMessage as error:
        raise OpError(ErrorCode.CONFLICT, "that message id is stored for more than one source; use its seq") from error
    return answer.as_dict()
