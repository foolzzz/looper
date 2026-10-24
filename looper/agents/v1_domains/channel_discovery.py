"""Lark identity and people discovery, and dashboard gate notification selection."""

from .. import gate_service
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _INT, _BOOL
from ..channels import lark_settings, lark_bot_settings
from ..channels.lark_cli import LarkCliError
from ..ops import ErrorCode, OpError
from ..ops.service import _plain
from ..projects import RequestError

ORDER = 9


GATE_TARGET = _object({"channel": {"type": "string", "enum": ["telegram", "lark", "lark_bot"]},
                       "chat": _STR}, closed=True)
_TARGET = {**GATE_TARGET, "type": ["object", "null"]}
NOTIFY_TARGET = _object({"channel": _STR, "chat": _STR, "label": _STR, "channel_enabled": _BOOL})
GATE_SETTINGS = _object({"dashboard_target": _TARGET, "dashboard_target_problem": _NULL_STR,
                         "targets": _array(NOTIFY_TARGET)})
LARK_STATUS = _object({
    "bot": {**_object(dict.fromkeys(("name", "app_id", "open_id", "profile"), _STR)),
            "type": ["object", "null"]},
    "problem": _NULL_STR, "allowlist_app_id": _NULL_STR,
    "names": {"type": "object", "additionalProperties": _STR},
    "rejected": _array(_object({"open_id": _STR, "name": _NULL_STR, "count": _INT,
                                "last_at": _STR, "chat_id": _NULL_STR}))})
LARK_PERSON = _object(dict.fromkeys(("open_id", "name", "email", "department"), _STR))
_CHANNEL = {"channel_type": {"type": "string", "enum": ["lark", "lark_bot"]},
            "channel_id": {"type": "string", "minLength": 1}}


def _op(name, request, result, risk="read"):
    description = {"channels.status": "Read a Lark bot's identity and sender status.",
                   "channels.people": "Search a Lark bot's people directory.",
                   "gate_settings.get": "Read dashboard gate notification settings.",
                   "gate_settings.update": "Change the dashboard gate notification target."}[name]
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), description, description,
                     request, result, risk=risk, confirms=risk == "high")


OPERATIONS = (
    _op("channels.status", _object(_CHANNEL, ["channel_type"], closed=True), LARK_STATUS),
    _op("channels.people", _object({**_CHANNEL, "q": {"type": "string", "minLength": 1,
            "maxLength": lark_settings.QUERY_MAX_CHARS}}, ["channel_type", "q"], closed=True),
        _object({"people": _array(LARK_PERSON), "complete": _BOOL})),
    _op("gate_settings.get", _object({}, closed=True), GATE_SETTINGS),
    _op("gate_settings.update", _object({"dashboard_target": _TARGET}, closed=True), GATE_SETTINGS, "high"),
)
RESULTS = {}


async def impact(server, operation, arguments, principal, headers):
    target = arguments["dashboard_target"]
    if target is None:
        return "Clear the dashboard gate notification target; gates will appear on the task page only."
    change = f"Send dashboard task gate notifications to {target['channel']} {target['chat']}?"
    if principal.kind == "operator":
        return change  # The single-call write uses the service's existing validation and errors.
    settings = await gate_service.gate_settings()
    offered = next((item for item in settings["targets"] if
                    (item["channel"], item["chat"]) == (target["channel"], target["chat"])), None)
    if offered is None or not offered["channel_enabled"]:
        raise OpError(ErrorCode.INVALID, "the target must be offered by an enabled channel's sender allowlist")
    return change


async def run(server, operation, arguments, principal, headers):
    name = operation.operation_id
    try:
        if name == "gate_settings.get":
            return await gate_service.gate_settings()
        if name == "gate_settings.update":
            # Confirmation reserves nothing; the unchanged service checks current targets before writing.
            return await gate_service.update_gate_settings(arguments)
        settings = lark_settings if arguments["channel_type"] == "lark" else lark_bot_settings
        channel_id = arguments.get("channel_id", settings.CHANNEL_ID)
        if name == "channels.status":
            return _plain(await settings.status(channel_id))
        result = await settings.people(arguments["q"], channel_id)
        return {**result, "complete": result.get("complete", True)}
    except LookupError as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    except (ValueError, RequestError) as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
    except LarkCliError as exc:
        raise OpError(ErrorCode.UNAVAILABLE, str(exc)) from None
