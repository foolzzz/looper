"""Operator-only switch for account admission, independent of chat settings."""

from ..db import system_settings
from . import admission, failures
from .ops import ErrorCode, PublicOpError

KEY = "engine.admission.enabled"


def view() -> dict[str, bool]:
    return {"enabled": admission.enabled()}


async def update(value: bool | None) -> dict[str, bool]:
    if value is not None and type(value) is not bool:
        raise ValueError("enabled must be true, false or null")
    if value and await failures.typing_mode() is failures.FailureTyping.OFF:
        raise PublicOpError(
            ErrorCode.CONFLICT,
            "Enable failure typing before account admission can freeze quota failures.",
        )
    await system_settings.put_settings(
        {KEY: None if value is None else str(value).lower()}
    )
    await admission.set_enabled(bool(value))
    return view()
