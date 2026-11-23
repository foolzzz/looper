"""Operator-owned ACP Reviewer settings, never exposed to Manager tools."""

from .. import acp_reviewer_settings as settings
from ..action_levels import ActionLevel
from ..ops import ErrorCode, OpError
from ..api_v1 import Operation, _BOOL, _STR, _object

_ARRAY = {"type": "array", "items": _STR, "maxItems": 100}
_FIELDS = {"enabled": _BOOL, "command": _ARRAY, "model": _STR, "account": _STR,
           "config_dir": _STR, "inspect_roots": _ARRAY}
_LAUNCH_CHECK = _object({"reason": _STR, "detail": _STR, "mechanism": _STR}, required=("reason",), closed=True)
VIEW = _object({**_FIELDS, "policy_check": _object({"reason": _STR, "detail": _STR}, closed=True),
                "latest_launch_check": {"anyOf": [_LAUNCH_CHECK, {"type": "null"}]}}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("acp_reviewer.get", "POST", "/api/v1/acp_reviewer/get", "Read the ACP Reviewer pilot settings.",
              "Read the ACP Reviewer pilot settings. The pilot is disabled by default; the policy is checked again at launch.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # Reads only Looper's own configuration.
    Operation("acp_reviewer.update", "POST", "/api/v1/acp_reviewer/update", "Configure the ACP Reviewer pilot.",
              "Configure the ACP Reviewer pilot. Enabling lets the Reviewer run the configured command under the write policy.",
              _object(_FIELDS, required=(), closed=True), VIEW, risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # Stores only Looper's own configuration; launch enforces the boundary.
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "acp_reviewer.update":
        return None
    return ("Update the ACP Reviewer pilot settings? Turning it on lets the Reviewer role run the configured "
            "command under the process write policy. Unknown CLI configurations or external MCP services refuse to start.")


async def run(server, operation, arguments, principal, headers):
    try:
        if operation.operation_id == "acp_reviewer.get":
            return await settings.view()
        return await settings.update(arguments)
    except ValueError as exc:
        raise OpError(ErrorCode.INVALID, str(exc)) from None
