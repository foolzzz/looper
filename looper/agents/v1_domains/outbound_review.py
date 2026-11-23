"""The outbound review's system setting (``outbound_hook.ENABLED_KEY``): whether every tool call of the Managers is
checked before it runs, and which enabled Managers run on a runtime the review cannot hook; and the operator's
declarations of the MCP tools that act only inside (``mcp_tool_levels``), which run without a review."""

from .. import outbound_hook
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _array, _object, _summary, _BOOL, _STR
from ..ops import ErrorCode, OpError

_MANAGER = _object({"manager_id": _STR, "name": _STR, "runtime": _STR}, closed=True)
SETTINGS = _object({"enabled": _BOOL, "managers_without_hook": _array(_MANAGER)}, closed=True)
DECLARED = _object({"item_id": _STR, "inside_tools": _array(_STR)}, closed=True)
SCHEMAS = {"OutboundReviewSettings": SETTINGS, "McpToolLevels": DECLARED}
RESULTS = {}


def _op(name, description, request, result, risk="read"):
    return Operation(name, "POST", "/api/v1/" + name.replace(".", "/"), _summary(description), description,
                     request, result, risk=risk, confirms=risk == "high", operator_only=True,
                     level=ActionLevel.INSIDE)  # reads or changes only Looper's review settings and MCP trust declarations


OPERATIONS = (
    _op("outbound_review.get", "Read whether the outbound review is on, and the enabled Managers it cannot hook. "
                               "The review cannot be turned on while an enabled Manager's runtime has no tool hook.",
        _object({}, closed=True), SETTINGS),
    _op("outbound_review.update", "Turn the outbound review on or off. On, each tool call of a Manager that may "
                                  "leave this machine is checked by the outbound_check role first.",
        _object({"enabled": _BOOL}, ["enabled"], closed=True), SETTINGS, "high"),
    _op("mcp_tool_levels.get", "Read the tools of an MCP server declared inside. Their calls run without the "
                               "outbound review; every other tool of the server is reviewed.",
        _object({"item_id": _STR}, ["item_id"], closed=True), DECLARED),
    _op("mcp_tool_levels.put", "Declare exactly these tools of an MCP server inside, acting only on this machine. "
                               "Their calls run without the outbound review.",
        _object({"item_id": _STR, "inside_tools": {**_array(_STR), "maxItems": 200}}, ["item_id", "inside_tools"],
                closed=True), DECLARED, "high"),
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id == "mcp_tool_levels.put":
        tools = ", ".join(arguments["inside_tools"]) or "none"
        return (f"Declare these tools of MCP server {arguments['item_id']!r} inside: {tools}? Their calls run without "
                "the outbound review; every other tool of the server is reviewed.")
    if operation.operation_id != "outbound_review.update":
        return None
    if arguments["enabled"]:
        return ("Turn the outbound review on? Every shell command and outbound call of the Managers is checked by "
                "the outbound_check role before it runs (a few seconds and about 0.003 USD each); a call it cannot "
                "judge is blocked. Running Managers restart on their next turn.")
    return ("Turn the outbound review off? The Managers' shell commands and outbound calls run without a check, and "
            "running Managers restart on their next turn.")


async def run(server, operation, arguments, principal, headers):
    from ...db import mcp_tool_levels

    if operation.operation_id == "outbound_review.get":
        return await outbound_hook.settings()
    try:
        if operation.operation_id == "mcp_tool_levels.get":
            return await mcp_tool_levels.get(arguments["item_id"])
        if operation.operation_id == "mcp_tool_levels.put":
            return await mcp_tool_levels.put(arguments["item_id"], arguments["inside_tools"])
        settings = await outbound_hook.set_enabled(arguments["enabled"])
        if arguments["enabled"] and getattr(server, "_managers", None) is not None:
            # A Manager process started without the hook stops now: its next turn starts one with it.
            await server._managers.require_tool_hooks()
        return settings
    except outbound_hook.HookUnavailable as exc:
        raise OpError(ErrorCode.CONFLICT, str(exc)) from None
    except mcp_tool_levels.NoSuchServer as exc:
        raise OpError(ErrorCode.NOT_FOUND, str(exc)) from None
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
