"""The idle check switch (assistant phase 3 RFC, 4.8): ``manager.idle_checks.enabled``.

Operator-only, in its own operations (``idle_check_settings.get`` and ``.update``), never ``chat_settings``. No row is
off. Turning it on needs what the checks stand on: the delivery layer (``delivery.enabled``: the quiet hours and the
daily cap bind what a check says), the needs-you summary (``attention.enabled``: where the candidates come from, and
what the message points to) and the lanes (``manager.lanes.enabled``: a check in progress is interrupted when the
operator writes). ``update(true)`` answers ``conflict`` and names those that are off. Turning one of them off later
does not turn this off: the service starts no new check while one is missing.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..db import system_settings
from .ops import ErrorCode, OpError

KEY = "manager.idle_checks.enabled"
DELIVERY_KEY = "delivery.enabled"
ATTENTION_KEY = "attention.enabled"
LANES_KEY = "manager.lanes.enabled"  # owned by the lanes switch; read here only
PREREQUISITES = (DELIVERY_KEY, ATTENTION_KEY, LANES_KEY)


async def settings(db=None) -> Mapping[str, bool]:
    """The switch and its three prerequisites, each ``True`` only for a stored ``true``."""
    stored = await system_settings.get_settings([KEY, *PREREQUISITES], pool=db)
    return {key: stored.get(key) == "true" for key in (KEY, *PREREQUISITES)}


async def enabled(db=None) -> bool:
    return (await system_settings.get_settings([KEY], pool=db)).get(KEY) == "true"


async def view() -> dict[str, bool]:
    return {"enabled": await enabled()}


async def update(value: bool | None) -> dict[str, bool]:
    """Store the switch (``None`` restores the default, off); ``conflict`` when turning it on while a prerequisite
    is off."""
    if value is not None and not isinstance(value, bool):
        raise ValueError("enabled must be true, false or null")
    if value:
        current = await settings()
        missing = [key for key in PREREQUISITES if not current[key]]
        if missing:
            raise OpError(ErrorCode.CONFLICT, "Cannot enable idle checks. Enable these prerequisites first: "
                          + ", ".join(missing))
    await system_settings.put_settings({KEY: None if value is None else str(value).lower()})
    return await view()
