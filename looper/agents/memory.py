"""Memory candidates as the dashboard, the API and the CLI show them (memory-write-path RFC, section 9): one view of a
candidate, one of its detail, and the list filters. Operator-only: a candidate holds the operator's words.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..db import memory as db_memory
from . import memory_model as model
from .memory_model import State

# The states ``open`` lists: what is not written, rejected or dropped (an expired one can be revived).
OPEN_STATES = (State.DRAFTING, State.PENDING, State.CONFIRMED, State.WRITING, State.EXPIRED, State.CONFLICT,
               State.BLOCKED, State.HELD, State.IN_EFFECT, State.REVOKED, State.SUPERSEDED)
LIST_FILTERS = ("open", "all", *(state.value for state in State))


def states_for(name: str) -> list[State] | None:
    """The states a list filter names (``open``, ``all`` or one state); None for all of them."""
    if name == "all":
        return None
    if name == "open":
        return list(OPEN_STATES)
    if name not in set(State):
        raise ValueError(f"unknown state filter {name!r}: one of {', '.join(LIST_FILTERS)}")
    return [State(name)]


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: int | None) -> str | None:
    return model.format_id(value) if value is not None else None


def candidate_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": model.format_id(row["id"]), "kind": row["kind"], "state": row["state"], "reason": row["reason"],
        "destination": row["destination"], "source_kind": row["source_kind"], "source_class": row["source_class"],
        "statement": row["statement"], "why": row["why"], "keywords": list(row["keywords"]), "title": row["title"],
        "note_path": row["note_path"], "revision": row["revision"], "content_hash": row["content_hash"],
        "untrusted": row["untrusted"], "flagged": row["flagged"], "hard_rule": row["hard_rule"],
        "project_id": row["project_id"], "manager_id": row["manager_id"], "commit": row["commit_sha"],
        "merged_into": _id(row["merged_into"]), "supersedes": _id(row["supersedes"]),
        "expires_at": _time(row["expires_at"]), "valid_until": _time(row["valid_until"]),
        "written_at": _time(row["written_at"]), "purged": row["purged_at"] is not None,
        "sources_total": int(row.get("sources_total") or 0),
        "created_at": _time(row["created_at"]), "updated_at": _time(row["updated_at"]),
    }


def _source_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"kind": row["kind"], "source_class": row["source_class"], "quote": row["quote"],
            "channel": row["channel"], "chat": row["chat"], "message_id": row["message_id"],
            "said_at": _time(row["said_at"]), "task_id": row["task_id"],
            "lead": f"lead-{row['lead_id']}" if row["lead_id"] is not None else None}


def _revision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"revision": row["revision"], "made_by": row["made_by"], "title": row["title"],
            "changes": list(row["changes"]), "diff_stats": dict(row["diff_stats"]),
            "large_removal": row["large_removal"], "content_hash": row["content_hash"],
            "created_at": _time(row["created_at"])}


def _event_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {"action": row["action"], "actor": row["actor_kind"], "via": row["via"], "revision": row["revision"],
            "content_hash": row["content_hash"], "note": row["note"], "at": _time(row["created_at"])}


def detail_view(found: Mapping[str, Any]) -> dict[str, Any]:
    """``db.memory.get``'s answer as one JSON object."""
    sources = found["sources"]
    return {**candidate_view({**found["candidate"], "sources_total": len(sources)}),
            "current": _revision_view(found["revision"]) if found["revision"] else None,
            "sources": [_source_view(row) for row in sources],
            "events": [_event_view(row) for row in found["events"]]}


async def list_view(*, state: str = "open", kind: str | None = None, limit: int = 50, before: str | None = None,
                    db: Any = None) -> dict[str, Any]:
    rows, more = await db_memory.list_candidates(
        states=states_for(state), kind=model.Kind(kind) if kind is not None else None, limit=limit,
        before_id=model.parse_id(before) if before is not None else None, db=db)
    return {"candidates": [candidate_view(row) for row in rows], "more": more}


async def get_view(candidate: str, *, db: Any = None) -> dict[str, Any] | None:
    found = await db_memory.get(model.parse_id(candidate), db=db)
    if found is None:
        return None
    view = detail_view(found)
    if view["current"] is not None:
        # Additive: what the page shows per file, made from the vault as it is now (memory_diff).
        from . import memory_diff
        diffs, problem = await memory_diff.revision_diffs(view["state"], view["current"]["changes"], db)
        view["current"] = {**view["current"], "diffs": diffs, "diffs_problem": problem}
    return view
