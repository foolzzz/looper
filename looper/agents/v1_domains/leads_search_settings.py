"""Operator-only message lead search mode, separate from extraction settings."""

from ...db import system_settings
from .. import leads_model
from ..action_levels import ActionLevel
from ..api_v1 import Operation, _object

MODE = {"type": "string", "enum": [mode.value for mode in leads_model.SearchMode]}
VIEW = _object({"mode": MODE}, closed=True)
RESULTS = {}
OPERATIONS = (
    Operation("leads_search_settings.get", "POST", "/api/v1/leads_search_settings/get",
              "Read the message lead search mode.",
              "Read the message lead search mode. The default is words; fulltext enables broader matching.",
              _object({}, closed=True), VIEW, risk="read", operator_only=True,
              level=ActionLevel.INSIDE),  # reads only Looper's feature setting
    Operation("leads_search_settings.update", "POST", "/api/v1/leads_search_settings/update",
              "Set the message lead search mode.",
              "Set the message lead search mode. Choose words or fulltext; null restores words. This affects "
              "subsequent searches on every surface, without enabling lead extraction or rewriting stored leads.",
              _object({"mode": {"type": ["string", "null"], "enum": [*MODE["enum"], None]}}, closed=True), VIEW,
              risk="high", confirms=True, operator_only=True,
              level=ActionLevel.INSIDE),  # changes matching of records already in Looper
)


async def impact(server, operation, arguments, principal, headers):
    if operation.operation_id != "leads_search_settings.update":
        return None
    value = arguments["mode"]
    return (f"Set leads.search_mode to {value or 'words'}"
            f"{' (built-in default)' if value is None else ''}? "
            "Fulltext broadens matching in subsequent CLI, dashboard and Manager searches. Words restores exact "
            "word matching. This does not enable lead extraction.")


async def run(server, operation, arguments, principal, headers):
    if operation.operation_id == "leads_search_settings.update":
        await system_settings.put_settings({leads_model.LEADS_SEARCH_MODE_KEY: arguments["mode"]})
    return {"mode": (await leads_model.search_mode()).value}
