"""``attention.summary``: what needs the operator now (``agents.attention``), for the dashboard's Needs you page.

Read-only and the operator's alone: it lists intents and the Manager's confirmations, which an API client does not
see. While ``attention.enabled`` is off it answers ``conflict`` (the dashboard's 409).
"""

from .. import attention
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _OBJ, _BOOL
from ..ops import ErrorCode, OpError
from ..ops.service import _plain

_ITEM = _object({"key": _STR, "title": _STR, "context": _STR, "since": _NULL_STR, "href": _NULL_STR,
                 "ref": _NULL_STR, "facts": _OBJ}, closed=True)
_SECTION = _object({"kind": {"type": "string", "enum": list(attention.KINDS)},
                    "state": {"type": "string", "enum": ["ok", "error", "unknown"]}, "count": _INT,
                    "items": _array(_ITEM), "error": _NULL_STR}, closed=True)
SUMMARY = _object({"read_at": _STR, "total": _INT, "sections": _array(_SECTION)}, closed=True)

RESULTS = {}
OPERATIONS = (
    Operation("attention.settings_get", "POST", "/api/v1/attention/settings_get",
              "Read whether the needs-you summary is enabled.",
              "Read whether the needs-you summary is enabled. Disabled by default; reads no summary sources.",
              _object({}, closed=True), _object({"enabled": _BOOL}, closed=True), operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's own feature setting
    Operation("attention.summary", "POST", "/api/v1/attention/summary",
              "Read what needs the operator now.",
              "Read what needs the operator now. The sections: user gates waiting for an answer, a Manager's "
              "operations waiting for confirmation, failing schedules, failing runtime logins, pending intents, the "
              "ledger's items that wait for the user, terminal reminder failures, and messages that may not have arrived. Each section is ok, error (its source "
              "could not be read) or unknown, with its count and up to 20 items; a feature that is off leaves its "
              "section out. Read-only: nothing is stored and no model runs. Answers conflict while "
              "attention.enabled is off.",
              _object({}, closed=True), SUMMARY, risk="read", operator_only=True),
)


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "attention.settings_get":
        return {"enabled": await attention.enabled()}
    if not await attention.enabled():
        raise OpError(ErrorCode.CONFLICT, "the needs-you summary is not enabled (system setting attention.enabled)")
    return _plain(await attention.collect(managers=getattr(server, "_managers", None)))
