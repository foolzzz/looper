"""Read the current, non-deleted capability set definitions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import asyncpg

from .connection import get_pool


async def current(ids: Sequence[str], *, pool: asyncpg.Pool | asyncpg.Connection | None = None
                  ) -> dict[str, tuple[int, Any]]:
    rows = await (pool or get_pool()).fetch(
        "SELECT s.id, s.current_version, v.items FROM capability_sets s "
        "JOIN capability_set_versions v ON v.set_id = s.id AND v.version = s.current_version "
        "WHERE s.id = ANY($1::text[]) AND s.deleted_at IS NULL", list(ids))
    return {row["id"]: (row["current_version"], json.loads(row["items"]) if isinstance(row["items"], str)
                        else row["items"]) for row in rows}
