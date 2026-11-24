"""Operator lesson reads and settings for asynchronous project retrospectives."""
from __future__ import annotations

import json
from typing import Any
from collections.abc import Mapping

from ..db import memory_lessons as store, projects, system_settings
from . import memory, memory_lessons_model as model, memory_model
from .memory_settings import _push_channel_problem
from .ops import ErrorCode, OpError


# Loaded before task recovery; successful operator updates refresh the switch, like the morning brief.
_enabled = False


def enabled() -> bool:
    return _enabled


async def load(db: Any = None) -> None:
    global _enabled
    _enabled = (await settings_view(db))["enabled"]


async def list_view(*, project: str | None = None, state: str = "all", limit: int = 50,
                    before: str | None = None, db: Any = None) -> dict[str, Any]:
    if state != "all" and state not in memory_model.LOOPER_STATES:
        raise ValueError("state must be all or a lesson state")
    before_id = memory_model.parse_id(before) if before is not None else None
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("limit must be a whole number from 1 to 200")
    if project is not None and await projects.get_project(project, pool=db) is None:
        raise OpError(ErrorCode.NOT_FOUND, f"no project {project}")
    rows, more = await store.list_lessons(project_id=project, state=None if state == "all" else state,
                                         limit=limit, before_id=before_id, db=db)
    lessons = []
    for row in rows:
        stats = dict(row["stats"])
        for key in ("last_injected_at", "last_cited_at"):
            stats[key] = stats[key].isoformat() if stats[key] is not None else None
        lessons.append({"candidate": memory.candidate_view(row["candidate"]), "stats": stats})
    return {"lessons": lessons, "more": more}


async def settings_view(db: Any = None) -> dict[str, Any]:
    value = (await system_settings.get_settings([model.SETTING_KEY], pool=db)).get(model.SETTING_KEY)
    problem = None
    try:
        raw = json.loads(value) if value is not None else {}
        if not isinstance(raw, Mapping):
            raise ValueError("expected an object")
        settings = model.parse_settings(raw)
        problem = await _push_channel_problem(settings.weekly.push_channel, db)
    except (ValueError, TypeError) as error:
        problem = f"invalid memory_lessons setting: {error}"
        settings = model.parse_settings({})
    if problem:
        # Runtime consumers must use enabled, and never act on an invalid stored configuration.
        settings = model.parse_settings({})
    return {"configured": value is not None, "enabled": settings.enabled and problem is None,
            "settings": settings.to_json(), "problem": problem}


async def _checked(body: Mapping[str, Any], db: Any = None):
    settings = model.parse_settings(body)
    if problem := await _push_channel_problem(settings.weekly.push_channel, db):
        raise ValueError(problem)
    return settings


async def update_settings(body: Mapping[str, Any], *, db: Any = None) -> dict[str, Any]:
    global _enabled
    settings = await _checked(body, db)
    from datetime import datetime, timezone
    from ..db import connection, memory_retro

    async with (db or connection.get_pool()).acquire() as conn, conn.transaction():
        if settings.enabled:
            await memory_retro.activate(conn, now=datetime.now(timezone.utc))
        await conn.execute("INSERT INTO system_settings(key,value) VALUES($1,$2) ON CONFLICT(key) "
                           "DO UPDATE SET value=EXCLUDED.value,updated_at=now()",
                           model.SETTING_KEY, json.dumps(settings.to_json(), allow_nan=False))
    _enabled = settings.enabled
    return await settings_view(db)


async def confirmation_impact(body: Mapping[str, Any], *, db: Any = None) -> str | None:
    settings = await _checked(body, db)
    current = await settings_view(db)
    old = model.parse_settings(current["settings"])
    if ((settings.auto_activate and not old.auto_activate) or
            (settings.enabled and settings.weekly.enabled and
             settings.weekly.push_channel != old.weekly.push_channel)):
        return ("Allow future task lessons to affect later tasks in the same project without individual operator "
                "review? Push channel: " + (settings.weekly.push_channel or "none") +
                ". Retrospectives run after enabled project tasks finish. Each attempt is capped at $0.10 by default; "
                "65 terminal tasks per day would reserve up to $6.50 before retries, subject to the daily budget. "
                "Actual cost depends on enabled projects and model pricing. Injection and delivery ship separately.")
    return None
