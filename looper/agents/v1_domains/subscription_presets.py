"""The fields an extraction subscription (message leads, intents) is created from, as the dashboard's presets fill
the schedule form (message-leads-intents RFC, section 9): ``subscriptions.preset`` for the only message source and
the server's zone."""

from ...db import message_stream as db_message_stream
from .. import cron, subscriptions
from ..api_v1 import Operation, _object, _OBJ
from ..ops import ErrorCode, OpError
from .schedule_reads import _enum

RESULTS = {}
_SUMMARY = "Read the schedule fields a message-lead, intent or event-triage subscription is created from."
_DESCRIPTION = (f"{_SUMMARY} They are a complete fields object for schedule_admin.create, except project_id and, for "
                "intents, notify_target (a direct chat). Its chat kinds, sender kinds and rendering only satisfy "
                "validation: an extraction's scope is fixed by its output kind.")
OPERATIONS = (
    Operation("schedule_admin.subscription_preset", "POST", "/api/v1/schedule_admin/subscription_preset",
              _SUMMARY, _DESCRIPTION,
              _object({"output_kind": _enum("leads", "intents", "triage")}, ["output_kind"], closed=True),
              _object({"fields": _OBJ}, ["fields"], closed=True), risk="read", operator_only=True),
)


async def run(server, operation, arguments, principal, headers):
    sources = await db_message_stream.list_sources()
    if len(sources) != 1:
        found = ", ".join(source["id"] for source in sources) or "none"
        raise OpError(ErrorCode.CONFLICT, f"a preset reads the only message source; there are {len(sources)} "
                                          f"({found}): set one up on the Message stream page")
    return {"fields": subscriptions.preset(arguments["output_kind"], source_id=sources[0]["id"],
                                           zone_name=cron.local_zone_name())}
