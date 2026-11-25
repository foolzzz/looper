"""Fixed recovery guidance for an interrupted Worker attempt; opt-in, default off."""

from enum import StrEnum
from typing import Any, Mapping

from ..db import llm_records, system_settings
from .kept_dirs import Reason
from .kept_run import AdoptFailure

KEY = "worker.interrupted_hint.enabled"


class InterruptionReason(StrEnum):
    DEPLOY_DRAIN = "deploy drain"
    RESTART_NOT_ADOPTED = "restart not adopted"
    RUN_STOPPED = "run stopped"


# Exact legacy record values only. Never interpolate free-form errors into a prompt.
_RESTART_ERRORS = {llm_records.UNFINISHED_AT_START, *(reason.value for reason in Reason),
                   *(f"the run could not be adopted after a restart: {failure.value}" for failure in AdoptFailure)}


def reason_for(record: Mapping[str, Any]) -> InterruptionReason:
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        try:
            return InterruptionReason(metadata.get("interruption_reason"))
        except (ValueError, TypeError):
            pass
    return (InterruptionReason.RESTART_NOT_ADOPTED if record.get("error") in _RESTART_ERRORS
            else InterruptionReason.RUN_STOPPED)


def instruction(reason: InterruptionReason) -> str:
    return (f"This step was interrupted before ({reason.value}); the workspace may hold partial changes from that run. "
            "Check git status and the files you will touch before continuing, and do not repeat external side effects "
            "(pushes, comments, messages) without checking whether they already happened.")


async def enabled() -> bool:
    return (await system_settings.get_settings([KEY])).get(KEY) == "true"


async def view() -> dict[str, bool]:
    return {"enabled": await enabled()}


async def update(value: bool | None) -> dict[str, bool]:
    if value is not None and type(value) is not bool:
        raise ValueError("enabled must be true, false or null")
    await system_settings.put_settings({KEY: None if value is None else str(value).lower()})
    return await view()
