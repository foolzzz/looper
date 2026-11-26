"""Operator-only access to retained proactive send bodies."""
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _INT, _NULL_STR, _STR
from ..ops import proactive_sends
from ..turn_metrics import ProactiveSource

_ROW = _object({"id": _INT, "source": _STR, "source_ref": _NULL_STR, "manager_id": _NULL_STR,
    "channel_id": _STR, "chat": _STR, "chat_kind": _STR, "dedupe_key": _NULL_STR,
    "local_date": _STR, "sent_at": _NULL_STR, "outcome": _NULL_STR,
    "body": _NULL_STR, "body_truncated": _BOOL, "body_stored_at": _NULL_STR}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("proactive_sends.list", "POST", "/api/v1/proactive_sends/list",
        "Read recent proactive sends and their retained text.",
        "Read recent proactive sends and their retained text. Operator only. Filter by source or local_date "
        "in the operator timezone; use next_before for the next page. Bodies are bounded to 32 KiB and cleared "
        "after 14 days; older records have null bodies.",
        _object({"source": {"type": "string", "enum": [source.value for source in ProactiveSource]},
            "local_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            "before": {"type": "integer", "minimum": 1}}, [], closed=True),
        _object({"deliveries": _array(_ROW), "next_before": {"type": ["integer", "null"]}}, closed=True),
        risk="read", operator_only=True,
        level=ActionLevel.INSIDE),  # reads only delivery data stored inside Looper
)


async def run(server, operation, arguments, principal, headers):
    return await proactive_sends.recent(arguments)
