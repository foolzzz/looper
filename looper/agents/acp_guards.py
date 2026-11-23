"""Selection boundaries for the operator-only ACP Reviewer pilot."""
from __future__ import annotations

from typing import Any

from .profile_registry import RegistryError
from .runtimes import RuntimeUnsupported


class AcpFallbackError(RuntimeUnsupported):
    """A stored fallback edge cannot enter or leave the ACP pilot."""


class AcpSelectionError(RegistryError):
    """An ACP selection must fail the definition, including optional template dependencies."""


def refuse_type(runtime_type: str, entry: str) -> None:
    if runtime_type == "acp":
        raise AcpSelectionError(f"ACP Reviewer pilot cannot be selected by {entry}; use the operator dashboard")


async def refuse_profile(wanted: str | None, entry: str, *, pool: Any = None) -> None:
    if not wanted:
        return
    from ..db import profile_registry as db

    rows = await db.list_profiles(pool=pool)
    row = next((row for row in rows if row["id"] == wanted), None) or next(
        (row for row in rows if row["name"].casefold() == wanted.strip().casefold()), None)
    if row is not None:
        refuse_type(row["runtime_type"], entry)


async def refuse_client_selection(principal: Any, entry: str, *, profile_id: str | None = None,
                                  runtime_id: str | None = None) -> None:
    """Dashboard domains share the pilot boundary when an API client calls them directly."""
    if principal.kind == "operator":
        return
    from ..db import profile_registry as db
    from .ops import ErrorCode, OpError

    try:
        await refuse_profile(profile_id, entry)
        if runtime_id:
            row = await db.get_runtime(runtime_id)
            refuse_type(row["type"] if row is not None else runtime_id, entry)
    except AcpSelectionError as exc:
        raise OpError(ErrorCode.FORBIDDEN, str(exc)) from None
