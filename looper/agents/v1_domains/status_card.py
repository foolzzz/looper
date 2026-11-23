"""Operator-only status card: the switch, and the snapshot the dashboard reads.

The card is off until ``status_card.enabled`` is true. ``status_card.get`` is the same snapshot ``/status`` sends.
"""

from datetime import datetime, timezone

from .. import status_card
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _BOOL, _NULL_STR, _STR
from ..ops import ErrorCode, OpError

VIEW = _object({"enabled": _BOOL}, closed=True)
_SECTION = _object({"name": _STR, "state": {"type": "string", "enum": ["ok", "error", "unknown"]},
                    "error": _NULL_STR}, required=["name", "state", "error"], closed=False)
CARD = _object({"read_at": _STR, "text": _STR, "sections": _array(_SECTION)}, closed=True)
RESULTS: dict[str, object] = {}
_GET = ("Read the status card snapshot.",
        "Read the status card snapshot. Code assembles it from Managers, running tasks, schedules, what needs the "
        "operator, and the next reminder. Answers conflict while status_card.enabled is off.")
_READ = ("Read whether the status card is on.",
         "Read whether the status card is on. It is off by default. Operator-only.")
_UPDATE = ("Turn the status card on or off.",
           "Turn the status card on or off. Null restores the default, off. On, /status is answered by code at once, "
           "only for the operator, and is not stored. Off, /status stays an ordinary message.")
OPERATIONS = (
    Operation("status_card_settings.get", "POST", "/api/v1/status_card_settings/get", _READ[0], _READ[1],
              _object({}, closed=True), VIEW, risk="read", operator_only=True, level=ActionLevel.INSIDE),
    Operation("status_card_settings.update", "POST", "/api/v1/status_card_settings/update", _UPDATE[0], _UPDATE[1],
              _object({"enabled": {"type": ["boolean", "null"]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True, level=ActionLevel.INSIDE),
    Operation("status_card.get", "POST", "/api/v1/status_card/get", _GET[0], _GET[1],
              _object({}, closed=True), CARD, risk="read", operator_only=True, level=ActionLevel.INSIDE),
)


def _impact_text(value: bool | None) -> str:
    enabled = False if value is None else bool(value)
    default = " (built-in default)" if value is None else ""
    return (f"Set status_card.enabled to {str(enabled).lower()}{default}? "
            "Turning it on makes /status a code snapshot for the operator only: Telegram stops asking the model, "
            "and Lark and console /status stop being ordinary messages. status_card.get starts working. "
            "Turning it off restores the current behavior.")


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "status_card_settings.update":
        return None
    return _impact_text(arguments["enabled"])


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "status_card_settings.get":
        return status_card.view()
    if operation.operation_id == "status_card_settings.update":
        return await status_card.update(arguments["enabled"])
    if not status_card.enabled():
        raise OpError(ErrorCode.CONFLICT, "the status card is not enabled (system setting status_card.enabled)")
    return await status_card.snapshot(fleet=getattr(server, "_managers", None),
                                     task_router=getattr(server, "_task_router", None),
                                     worker_pool=getattr(server, "_worker_pool", None),
                                     now=datetime.now(timezone.utc), source="get")
