"""Message-source alert episodes and notice claims. No message content is stored here."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

KINDS = frozenset({"parity_drift", "collector_failure", "stale", "auth", "auth_expiring", "partial"})
STATES = frozenset({"pending", "sending", "sent", "rejected", "unknown", "dashboard", "deferred"})
ERRORS = frozenset({"auth", "identity", "protocol", "pagination", "transient", "rate_limit", "permission", "left",
                    "budget", "interrupted", "connection", "policy", "target", "delivery", "unknown"})
METADATA = frozenset({"run_id", "status", "error", "error_code", "window_from", "window_to", "bridge_only",
                      "collect_only", "identity_conflicts", "partials"})


def _metadata(value: Mapping[str, Any] | None) -> str:
    value = {key: item for key, item in (value or {}).items() if item is not None}
    if value.keys() - METADATA:
        raise ValueError("unsupported source alert metadata")
    for key, item in value.items():
        if key in {"bridge_only", "collect_only", "identity_conflicts", "partials", "error_code"}:
            if item is not None and (type(item) is not int or item < 0):
                raise ValueError("invalid source alert count")
        elif key in {"window_from", "window_to"}:
            if isinstance(item, datetime):
                value[key] = item.isoformat()
            elif not isinstance(item, str):
                raise ValueError("invalid source alert window")
            parsed = datetime.fromisoformat(value[key])
            if parsed.tzinfo is None:
                raise ValueError("source alert window needs a timezone")
        elif key == "error":
            if item is not None and item not in ERRORS:
                raise ValueError("invalid source alert error")
        elif key == "status":
            if item not in {"running", "ok", "partial", "interrupted", "search_failed", "auth_failed", "failed", "skipped_busy"}:
                raise ValueError("invalid source alert status")
        elif key == "run_id" and (not isinstance(item, str) or not item.isascii() or not item.isdecimal()):
            raise ValueError("invalid source alert run id")
    return json.dumps(value)


def view(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = row["metadata"]
    metadata = json.loads(_metadata(json.loads(metadata) if isinstance(metadata, str) else dict(metadata)))
    return {"id": str(row["id"]), "kind": row["kind"], "opened_at": row["opened_at"],
            "resolved_at": row["resolved_at"],
            "parity_session_id": str(row["parity_session_id"]) if row["parity_session_id"] is not None else None,
            "metadata": metadata,
            "open_state": row["open_state"], "recovery_state": row["recovery_state"]}


async def configured(conn: Any) -> bool:
    return bool(await conn.fetchval("SELECT EXISTS (SELECT 1 FROM source_collection_settings WHERE alerts_enabled)"))


async def open_episode(conn: Any, source_id: str, kind: str, *, now: datetime | None = None,
                       parity_session_id: int | None = None, metadata: Mapping[str, Any] | None = None,
                       target_channel: str | None = None, target_chat: str | None = None) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("invalid source alert kind")
    row = await conn.fetchrow(
        "INSERT INTO source_stream_alerts (source_id, kind, opened_at, parity_session_id, metadata, "
        "target_channel, target_chat) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7) "
        "ON CONFLICT (source_id, kind) WHERE resolved_at IS NULL DO UPDATE SET kind = EXCLUDED.kind "
        "RETURNING *", source_id, kind, now or datetime.now(timezone.utc), parity_session_id, _metadata(metadata),
        target_channel, target_chat)
    return dict(row)


async def resolve_episode(conn: Any, source_id: str, kind: str, *, now: datetime,
                          parity_session_id: int | None = None) -> bool:
    return bool(await conn.fetchval(
        "UPDATE source_stream_alerts SET resolved_at = $3 WHERE source_id = $1 AND kind = $2 "
        "AND resolved_at IS NULL AND ($4::bigint IS NULL OR parity_session_id = $4) RETURNING true",
        source_id, kind, now, parity_session_id))


def _side(side: str) -> str:
    if side not in {"open", "recovery"}:
        raise ValueError("invalid source notice side")
    return side


async def claim_notice(conn: Any, alert_id: int, side: str, *, now: datetime) -> dict[str, Any] | None:
    side = _side(side)
    claim = uuid.uuid4()
    row = await conn.fetchrow(
        f"UPDATE source_stream_alerts SET {side}_state = 'sending', {side}_claim = $2, "
        f"{side}_attempts = {side}_attempts + 1 WHERE id = $1 AND {side}_state IN ('pending', 'rejected') "
        f"AND {side}_attempts < 5 AND ({side}_next_at IS NULL OR {side}_next_at <= $3) "
        + ("AND resolved_at IS NOT NULL " if side == "recovery" else "") + "RETURNING *", alert_id, claim, now)
    return dict(row) if row is not None else None


async def settle_notice(conn: Any, alert_id: int, side: str, claim: uuid.UUID, state: str, *, now: datetime,
                        error: str | None = None) -> bool:
    side = _side(side)
    if state not in STATES - {"pending", "sending"} or error is not None and error not in ERRORS:
        raise ValueError("invalid source notice result")
    return bool(await conn.fetchval(
        f"UPDATE source_stream_alerts SET {side}_state = $3, {side}_error = $4, {side}_claim = NULL, "
        f"{side}_next_at = $5 WHERE id = $1 AND {side}_claim = $2 AND {side}_state = 'sending' RETURNING true",
        alert_id, claim, state, error, now + timedelta(minutes=1) if state == "rejected" else None))


async def orphan_notices(conn: Any) -> None:
    """Only call while holding the global notice sender lock: no sender can still own these claims."""
    for side in ("open", "recovery"):
        await conn.execute(f"UPDATE source_stream_alerts SET {side}_state = 'unknown', "
                           f"{side}_error = 'unknown', {side}_claim = NULL WHERE {side}_state = 'sending'")


async def pending_notices(conn: Any, *, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT a.* FROM source_stream_alerts a JOIN source_collection_settings s USING (source_id) "
        "WHERE s.alerts_enabled AND ((open_state IN ('pending', 'rejected') AND open_attempts < 5 "
        "AND (open_next_at IS NULL OR open_next_at <= $1)) OR (resolved_at IS NOT NULL "
        "AND recovery_state IN ('pending', 'rejected') AND recovery_attempts < 5 "
        "AND (recovery_next_at IS NULL OR recovery_next_at <= $1))) ORDER BY opened_at, id LIMIT $2", now, limit)
    return [dict(row) for row in rows]


_VISIBLE = "(resolved_at IS NULL OR (resolved_at >= $1 AND (open_state = 'unknown' OR recovery_state = 'unknown')))"


async def list_active(conn: Any, *, source_id: str | None = None, since: datetime | None = None,
                      limit: int = 20) -> tuple[list[dict[str, Any]], int]:
    since = since or datetime.now(timezone.utc) - timedelta(hours=72)
    predicate = _VISIBLE + " AND ($2::text IS NULL OR source_id = $2)"
    count = await conn.fetchval("SELECT count(*) FROM source_stream_alerts WHERE " + predicate, since, source_id)
    rows = await conn.fetch("SELECT * FROM source_stream_alerts WHERE " + predicate +
                            " ORDER BY opened_at, id LIMIT $3", since, source_id, limit)
    return [dict(row) for row in rows], count


async def earliest_active(conn: Any, *, since: datetime | None = None) -> dict[str, Any] | None:
    rows, _ = await list_active(conn, since=since, limit=1)
    return rows[0] if rows else None
