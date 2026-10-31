"""The startup control-plane snapshot and current served dashboard (operator only)."""
from ... import components
from ..ops import ErrorCode, OpError
from ..api_v1 import Operation, _INT, _STR, _array, _object

_HASHES = {"type": "object", "additionalProperties": _STR}
_ADOPTION = _object({"run_manifest_version": _INT, "work_contract": _STR, "review_contract": _STR,
                     "seat_tools": _HASHES, "keeper_protocols": _array(_STR)})
RESULTS = {}
OPERATIONS = (Operation(
    "system.components", "POST", "/api/v1/system/components", "Read loaded component fingerprints.",
    "Read startup control-plane and adoption fingerprints, plus current served dashboard source and asset fingerprints.",
    None, _object({"version": _STR, "loaded_at": _STR, "components": _HASHES,
                   "adoption": _ADOPTION, "verification": _HASHES}), risk="read", operator_only=True,
    audit_operator=False),)


async def run(server, operation, arguments, principal, headers):
    if server._components is None:
        raise OpError(ErrorCode.UNAVAILABLE, "The startup component snapshot is not available yet")
    result = server._components.to_dict()
    try:
        source, assets = components.web_fingerprints(server._static_dir)
    except (OSError, ValueError, UnicodeError) as exc:
        raise OpError(ErrorCode.UNAVAILABLE, "Cannot read the served dashboard fingerprints") from exc
    result["components"]["web"] = source
    result["verification"]["web"] = assets
    return result
