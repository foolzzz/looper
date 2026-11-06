"""Recorded model exchanges, with request parts deduplicated within each route."""

from __future__ import annotations

import json
import logging
from copy import copy
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import asyncpg

from .connection import get_pool

if TYPE_CHECKING:
    from looper.agents.llmrec.capture import Capture

logger = logging.getLogger(__name__)


_COLUMNS = (
    "route_id", "proxy_instance", "proxy_seq", "record_id", "after_record_id", "agent_id", "task_id",
    "started_at", "method", "path", "kind", "model", "stream", "status", "outcome", "error", "ttfb_ms",
    "duration_ms", "request_headers", "response_headers", "request_body", "request_bytes", "request_sha256",
    "response_body", "response_raw", "response_bytes", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "usage", "record_gap", "body_sanitized",
)
_JSON_COLUMNS = {"request_headers", "response_headers", "request_body", "response_body", "usage"}
_LIST_COLUMNS = ", ".join(["id", *(c for c in _COLUMNS if c not in {"request_body", "response_body", "response_raw"})])


def _sanitize(value: Any, capture: Capture) -> Any:
    if isinstance(value, str):
        if "\0" in value:
            capture.body_sanitized = True
            return value.replace("\0", "\\u0000")
    elif isinstance(value, list):
        return [_sanitize(item, capture) for item in value]
    elif isinstance(value, dict):
        return {key: _sanitize(item, capture) for key, item in value.items()}
    return value


def _json(value: Any, capture: Capture | None = None) -> str | None:
    if capture is not None:
        value = _sanitize(value, capture)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None


def _time(value: datetime | str) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _row(row: asyncpg.Record) -> dict[str, Any]:
    result = dict(row)
    for name, value in result.items():
        if isinstance(value, datetime):
            result[name] = value.isoformat()
        elif name in _JSON_COLUMNS and isinstance(value, str):
            result[name] = json.loads(value)
    return result


async def write_batch(captures: list[Capture], *, pool: asyncpg.Pool) -> None:
    """Commit a batch with rejected bodies isolated; sequence retries are harmless."""
    if not captures:
        return
    values = [f"${i}::jsonb" if name in _JSON_COLUMNS else f"${i}"
              for i, name in enumerate(_COLUMNS, 1)]
    record_position = _COLUMNS.index("record_id") + 1
    values[record_position - 1] = f"(SELECT id FROM llm_records WHERE id = ${record_position})"
    statement = (f"INSERT INTO llm_requests ({', '.join(_COLUMNS)}) VALUES ({', '.join(values)}) "
                 "ON CONFLICT (proxy_instance, proxy_seq) DO NOTHING")
    async with pool.acquire() as conn, conn.transaction():
        async def write(capture):
            route = capture.route
            upstream = urlsplit(route.upstream)
            await conn.execute("""
                INSERT INTO llm_request_routes
                    (id, agent_id, task_id, step_id, role, runtime_type, provider_id, protocol, model,
                     upstream_host, upstream_path, created_at, last_request_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                ON CONFLICT (id) DO UPDATE SET last_request_at =
                    GREATEST(llm_request_routes.last_request_at, EXCLUDED.last_request_at)
                """, route.id, route.agent_id, route.task_id, route.step_id, route.role, route.runtime_type,
                route.provider_id, route.protocol, route.model, upstream.hostname or "", upstream.path,
                _time(route.created_at), _time(capture.started_at))
            if capture.parts:
                await conn.executemany("""
                    INSERT INTO llm_request_parts (route_id, sha256, body, bytes) VALUES ($1,$2,$3::jsonb,$4)
                    ON CONFLICT DO NOTHING
                    """, [(route.id, sha, _json(body, capture), len(_json(body).encode("utf-8")))
                          for sha, body in capture.parts.items()])
            args = []
            for name in _COLUMNS:
                if name == "route_id":
                    value = route.id
                elif name == "proxy_instance":
                    value = capture.instance
                elif name == "proxy_seq":
                    value = capture.seq
                elif name in {"agent_id", "task_id"}:
                    value = getattr(route, name)
                elif name == "request_body":
                    value = capture.skeleton
                else:
                    value = getattr(capture, name)
                if name in _JSON_COLUMNS:
                    value = _json(value, capture)
                elif name == "response_raw":
                    value = _sanitize(value, capture)
                elif name == "started_at":
                    value = _time(value)
                args.append(value)
            await conn.execute(statement, *args)

        for capture in captures:
            capture.body_sanitized = getattr(capture, "body_sanitized", False)
            try:
                # PostgreSQL aborts a failed transaction: roll back this row before its fallback.
                async with conn.transaction():
                    await write(capture)
            except asyncpg.DataError as error:
                capture.record_gap = "rejected_by_database"
                capture.error = type(error).__name__
                fallback = copy(capture)
                fallback.skeleton, fallback.parts = None, {}
                fallback.response_body = fallback.response_raw = fallback.usage = None
                try:
                    async with conn.transaction():
                        await write(fallback)
                except asyncpg.DataError as fallback_error:
                    # Even metadata may be invalid. Never let one rejected row block later captures.
                    logger.warning("model request metadata rejected; error_class=%s", type(fallback_error).__name__)


async def list_requests(*, record_id: int | None = None, agent_id: str | None = None,
                        task_id: str | None = None, route_id: str | None = None, between_turns: bool = False,
                        before_id: int | None = None, limit: int = 100,
                        pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """Return metadata only, newest first; between-turn requests follow the named record."""
    if between_turns and record_id is None:
        raise ValueError("between_turns requires record_id")
    pool = pool or get_pool()
    clauses, args = [], []
    if between_turns:
        clauses.append("record_id IS NULL")
    for column, value in (("after_record_id" if between_turns else "record_id", record_id),
                          ("agent_id", agent_id), ("task_id", task_id), ("route_id", route_id)):
        if value is not None:
            args.append(value)
            clauses.append(f"{column} = ${len(args)}")
    if before_id is not None:
        args.append(before_id)
        clauses.append(f"id < ${len(args)}")
    args.append(max(1, min(limit, 500)))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = await pool.fetch(f"SELECT {_LIST_COLUMNS} FROM llm_requests{where} ORDER BY id DESC LIMIT ${len(args)}",
                            *args)
    return [_row(row) for row in rows]


async def get_request(request_id: int, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """Read one complete exchange and restore its request's shared JSON parts."""
    from looper.agents.llmrec.capture import join_request

    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction(isolation="repeatable_read", readonly=True):
        row = await conn.fetchrow("SELECT * FROM llm_requests WHERE id = $1", request_id)
        if row is None:
            return None
        result = _row(row)
        references, pending = set(), [result["request_body"]]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if set(value) == {"$llmrec_part"}:
                    references.add(value["$llmrec_part"])
                else:
                    pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        parts = await conn.fetch(
            "SELECT sha256, body FROM llm_request_parts WHERE route_id = $1 AND sha256 = ANY($2::text[])",
            row["route_id"], list(references)) if references else []
        result["request_body"] = join_request(result["request_body"],
            {part["sha256"]: json.loads(part["body"]) for part in parts}, result["path"])
        return result
