"""Stored channel settings; bot credentials remain operator-only and outside the action audit."""

from ...db import channel_configs as db_configs
from ..api_v1 import Operation, _array, _object, _STR, _NULL_STR, _BOOL
from ..channels import bot_api, chat_routing
from ...db.connection import get_pool
from ..channels.allowlist import OPERATOR_FIELD, AllowlistError, SENDER_ID_FORMATS, channel_view, normalized_config
from ..ops import ErrorCode, OpError, PublicOpError
from ..projects import RequestError, HTTP_400, HTTP_404, HTTP_409

_ID = {"type": "string", "minLength": 1}
_TYPE = {"type": "string", "enum": ["telegram", "lark", "lark_bot"]}
_TELEGRAM_SENDERS = {"anyOf": [{"type": "null"}, _STR, _array({"type": ["string", "integer"]})]}
_LARK_SENDERS = {"anyOf": [{"type": "null"}, _STR, _array(_STR)]}
_COMMON = {"description": _STR, "operator_sender_id": _NULL_STR}
_LARK = {**_COMMON, "allowed_open_ids": _LARK_SENDERS, "allowlist_app_id": _NULL_STR}
CONFIGS = {
    "telegram": _object({**_COMMON, "bot_token_env": _NULL_STR,
                          "allowed_user_ids": _TELEGRAM_SENDERS,
                          "backlog_max_age_seconds": {"type": "integer", "minimum": 1}}, [], closed=True),
    "lark": _object({**_LARK, "profile": _NULL_STR}, [], closed=True),
    "lark_bot": _object({**_LARK, "app_id": _STR,
                          "domain": {"type": "string", "enum": ["feishu", "lark"]}}, [], closed=True),
}
CHANNEL_CONFIG_VALUES = {"type": "object", "anyOf": list(CONFIGS.values())}
_VIEW_FIELDS = {
    "channel_id": _STR, "channel_type": _TYPE, "display_name": _STR, "enabled": _BOOL, "bound_chats_only": _BOOL,
    "config": CHANNEL_CONFIG_VALUES, "manager_id": _STR, "manager_name": _NULL_STR,
    "builtin": _BOOL, "start_problem": _NULL_STR, "created_at": _STR, "updated_at": _STR,
}
CHANNEL_CONFIG = {**_object({**_VIEW_FIELDS, "has_bot_token": _BOOL,
    "runtime": {"type": ["object", "null"], "readOnly": True,
                "description": "Read channel startup state. Includes the last error and next attempt; null when disabled or not started here.",
                "properties": {"state": {"enum": ["starting", "running", "retrying", "failed"]},
                               "last_error": _NULL_STR, "next_attempt_at": _NULL_STR},
                "required": ["state", "last_error", "next_attempt_at"], "additionalProperties": False},
    }, list(_VIEW_FIELDS), closed=True),
    "anyOf": [_object({"channel_type": {"const": kind}, "config": config})
              for kind, config in CONFIGS.items()]}
SCHEMAS = {"ChannelConfigValues": CHANNEL_CONFIG_VALUES, "ChannelConfig": CHANNEL_CONFIG}
_FIELDS = {"display_name": _NULL_STR, "enabled": _BOOL, "config": CHANNEL_CONFIG_VALUES}
_TOKEN = {"type": "string", "writeOnly": True,
          "description": "Stored credential input only. Responses expose presence, never the credential."}


def _op(action, summary, request, result, *, risk="read", credential=False):
    return Operation("channel_configs." + action, "POST", "/api/v1/channel_configs/" + action,
                     summary, summary, request, result, risk=risk, confirms=risk == "high",
                     operator_only=credential, credential_write=credential)


OPERATIONS = (
    _op("list", "List channel settings with credential presence only.", _object({}, closed=True),
        _object({"configs": _array(CHANNEL_CONFIG)}, closed=True)),
    _op("get", "Read channel settings with credential presence only.",
        _object({"channel_id": _ID}, closed=True), CHANNEL_CONFIG),
    _op("create", "Add a disabled bot by default, owned by the given Manager; operator only.",
        {**_object({"channel_type": _TYPE, "manager_id": _ID, **_FIELDS, "bot_token": _TOKEN},
                  ["channel_type", "manager_id"], closed=True),
         "anyOf": [_object({"channel_type": {"const": kind}, "config": config}, ["channel_type"])
                   for kind, config in CONFIGS.items()]},
        CHANNEL_CONFIG, risk="write", credential=True),
    _op("update", "Update a bot without changing its Manager; operator only. Omitted or *** tokens keep; null or empty removes.",
        _object({"channel_id": _ID, "fields": _object({**_FIELDS, "manager_id": _ID,
            "bot_token": {**_TOKEN, "type": ["string", "null"]}}, [], closed=True)}, closed=True),
        CHANNEL_CONFIG, risk="write", credential=True),
    _op("delete", "Remove an added bot and its credential; keep chat history. Restart to stop its adapter.",
        _object({"channel_id": _ID}, closed=True),
        _object({"deleted": _STR, "restart": _BOOL}, closed=True), risk="high"),
)
RESULTS = {}


def _view(server, row):
    registry = getattr(server, "_channels", None)
    return {**channel_view(row), "runtime": registry.start_state(row["channel_id"])
            if registry is not None and row["enabled"] else None}


async def _get(channel_id, server=None):
    row = await db_configs.get_config(channel_id)
    if row is None:
        raise RequestError(HTTP_404, "channel not found")
    return _view(server, row)


async def impact(server, operation, arguments, principal, headers):
    row = await _get(arguments["channel_id"])
    if row["builtin"]:
        raise OpError(ErrorCode.INVALID, "a built-in channel cannot be removed; turn it off instead")
    if principal.kind == "client" and await get_pool().fetchval(
            "SELECT bound_chats_only OR EXISTS (SELECT 1 FROM manager_chat_bindings b "
            "WHERE b.channel_id = c.channel_id) FROM channel_configs c WHERE c.channel_id = $1",
            arguments["channel_id"]):
        raise OpError(ErrorCode.FORBIDDEN, "operator-managed chat routing prevents client deletion")
    return (f"Remove bot {row['channel_id']} ({row['display_name']}) from Manager {row['manager_id']}? "
            "Its stored credential and refused senders will be deleted; chat history stays. "
            "Restart Looper to stop its running adapter.")


def _check_allowlist(kind, config):
    fmt = SENDER_ID_FORMATS[kind]
    try:
        normalized_config(kind, {key: value for key, value in config.items() if key != OPERATOR_FIELD})
    except AllowlistError:
        # The service's error quotes the first bad entry, which may be a pasted
        # token. Keep its format hint, never the submitted entry.
        raise PublicOpError(ErrorCode.INVALID, "invalid sender allowlist: " + fmt.hint) from None
    try:
        normalized_config(kind, config)
    except AllowlistError:
        raise PublicOpError(ErrorCode.INVALID, f"invalid {OPERATOR_FIELD}: it must be one {fmt.label} of the "
                                               f"senders in {fmt.field}") from None


async def _save(request, *, creating=False):
    try:
        return await request
    except bot_api.BotValidationRefusal as exc:
        raise PublicOpError(ErrorCode.INVALID, exc.reason.value) from None
    except bot_api.BotIdentityUnavailable as exc:
        raise PublicOpError(ErrorCode.CONFLICT, str(exc)) from None
    except RequestError as exc:
        # bot_api's 4xx refusals can quote sender IDs, Manager IDs or stored bot
        # names. Use stable explanations without copying any submitted value.
        public = {
            HTTP_400: (ErrorCode.INVALID, "invalid channel configuration; check the sender allowlist and bot settings"),
            HTTP_404: (ErrorCode.NOT_FOUND, "Manager not found" if creating else "channel not found"),
            HTTP_409: (ErrorCode.CONFLICT, "this bot is already added; update the existing bot instead"),
        }
        if exc.status in public:
            raise PublicOpError(*public[exc.status]) from None
        # Preserve the service failure class without exposing raw exception text.
        # The transport's RequestError fallback would turn an unmapped 500 into 400.
        if exc.status.startswith("5"):
            raise PublicOpError(ErrorCode.UNAVAILABLE, "invalid credential request") from None
        raise


async def run(server, operation, arguments, principal, headers):
    action = operation.operation_id.rsplit(".", 1)[1]
    if action == "list":
        return {"configs": [_view(server, row) for row in await db_configs.list_configs()]}
    if action == "get":
        return await _get(arguments["channel_id"], server)
    if action == "create":
        _check_allowlist(arguments["channel_type"], arguments.get("config", {}))
        return _view(server, await _save(bot_api.add_bot(arguments), creating=True))
    if action == "update":
        fields = dict(arguments["fields"])
        if "config" in fields:
            from ..api_v1 import validate_arguments
            row = await _save(_get(arguments["channel_id"]))
            validate_arguments(_object({"config": CONFIGS[row["channel_type"]]}, closed=True),
                               {"config": fields["config"]})
            _check_allowlist(row["channel_type"], fields["config"])
        row = await _save(bot_api.update_bot(arguments["channel_id"], fields))
        if server._channels is not None and not row["enabled"]:
            await server._channels.cancel_start(row["channel_id"])
        return _view(server, row)
    # A pending confirmation reserves nothing: re-read the target immediately before deletion.
    await impact(server, operation, arguments, principal, headers)
    result = await chat_routing.remove_bot_with_routing(arguments["channel_id"],
        principal_kind=principal.kind, registry=server._channels)
    if result == "has_bindings":
        raise OpError(ErrorCode.FORBIDDEN, "operator-managed chat routing prevents client deletion")
    if result == "missing":
        raise OpError(ErrorCode.NOT_FOUND, "channel not found")
    if result == "builtin":
        raise OpError(ErrorCode.INVALID, "a built-in channel cannot be removed; turn it off instead")
    if server._channels is not None:
        await server._channels.cancel_start(arguments["channel_id"])
    return {"deleted": arguments["channel_id"], "restart": True}
