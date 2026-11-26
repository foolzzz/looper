"""Operator read of proactive send text; deliberately absent from agent tools."""
from datetime import date
from typing import Any, Mapping

from ...db import proactive_deliveries
from .. import proactive_delivery
from . import ErrorCode, OpError
from .service import _plain


async def recent(arguments: Mapping[str, Any]) -> dict[str, Any]:
    local_date = arguments.get("local_date")
    if local_date is not None:
        try:
            local_date = date.fromisoformat(local_date)
        except ValueError:
            raise OpError(ErrorCode.INVALID, "local_date must be a valid YYYY-MM-DD date") from None
    return _plain(await proactive_deliveries.recent_bodies(
        source=arguments.get("source"), local_date=local_date,
        timezone=proactive_delivery.load_policy().timezone,
        limit=arguments.get("limit", 50), before=arguments.get("before")))
