"""Operator configuration of the loopback Anthropic gateway."""

from looper.db import gateway as db, system_settings
from ..api_v1 import Operation, _array, _object, _STR, _INT
from ..llmrec.control import controller
from ..llmrec.gateway import GatewayKey
from ..ops import ErrorCode, OpError, PublicOpError

_BOOL = {"type": "boolean"}
_ALIAS = _object(
    {"alias": _STR, "model_id": _STR, "model_name": _STR, "provider_id": _STR, "provider_name": _STR, "enabled": _BOOL},
    closed=True,
)
SETTINGS = _object(
    {
        "enabled": _BOOL,
        "port": {"type": ["integer", "null"]},
        "url": {"type": ["string", "null"]},
        "key_exists": _BOOL,
        "aliases": _array(_ALIAS),
        "proxy": {"type": "object"},
        "translate": {"type": "object"},
        "auth_failures": _INT,
        "alias_usage": {"type": "object"},
    },
    closed=True,
)
RESULTS = {}


def _op(name, fields, result, required=None, risk="read"):
    description = {
        "get": "Read local gateway configuration and counters.",
        "update": "Enable or configure the loopback gateway.",
        "alias_set": "Set an alias for an enabled OpenAI Chat Completions model.",
        "alias_delete": "Delete a local gateway alias.",
        "key_reveal": "Reveal the local gateway key.",
        "key_rotate": "Replace the local gateway key; existing clients must update their key.",
    }[name]
    return Operation(
        "gateway." + name,
        "POST",
        "/api/v1/gateway/" + name,
        description,
        description,
        _object(fields, required, closed=True),
        result,
        risk=risk,
        operator_only=True,
        credential_write=name in ("key_reveal", "key_rotate"),
        audit_operator=name not in ("key_reveal", "key_rotate"),
    )


OPERATIONS = (
    _op("get", {}, SETTINGS),
    _op(
        "update",
        {"enabled": _BOOL, "port": {"type": "integer", "minimum": 1, "maximum": 65535}},
        SETTINGS,
        ["enabled"],
        risk="write",
    ),
    _op("alias_set", {"alias": _STR, "model_id": _STR}, SETTINGS, risk="write"),
    _op("alias_delete", {"alias": _STR}, SETTINGS, risk="write"),
    _op("key_reveal", {}, _object({"key": _STR}, closed=True), risk="write"),
    _op("key_rotate", {}, _object({"key": _STR}, closed=True), risk="write"),
)


async def settings(root):
    values = await system_settings.get_settings(["gateway.enabled", "gateway.port"])
    port = int(values["gateway.port"]) if values.get("gateway.port") else None
    status = await controller().status() if controller() is not None else {}
    return {
        "enabled": values.get("gateway.enabled") == "true",
        "port": port,
        "url": f"http://127.0.0.1:{port}/gw" if port else None,
        "key_exists": GatewayKey(root).exists(),
        "aliases": await db.list_aliases(),
        "proxy": status.get("proxy", {"state": "stopped"}),
        "translate": status.get("translate", {}),
        "auth_failures": status.get("auth_failures", 0),
        "alias_usage": status.get("alias_usage", {}),
    }


async def run(server, operation, arguments, principal, headers):
    root = server._runtime_root
    if root is None:
        raise OpError(ErrorCode.UNAVAILABLE, "local gateway runtime unavailable")
    action = operation.operation_id.rsplit(".", 1)[1]
    try:
        if action == "alias_set":
            await db.set_alias(arguments["alias"], arguments["model_id"])
        elif action == "alias_delete":
            await db.delete_alias(arguments["alias"])
        elif action in ("key_reveal", "key_rotate"):
            key = GatewayKey(root)
            if action == "key_rotate":
                return {"key": key.rotate()}
            if not key.exists():
                raise PublicOpError(ErrorCode.NOT_FOUND, "enable the local gateway to create its key")
            return {"key": key.reveal()}
        elif action == "update":
            control = controller()
            if control is None:
                raise OpError(ErrorCode.UNAVAILABLE, "local gateway controller unavailable")
            await control.set_gateway(arguments["enabled"], port=arguments.get("port"))
    except ValueError as error:
        raise OpError(ErrorCode.INVALID, str(error)) from None
    except OSError:
        raise OpError(ErrorCode.UNAVAILABLE, "local gateway configuration unavailable") from None
    return await settings(root)
