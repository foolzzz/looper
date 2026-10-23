"""API clients: the local agents and scripts that call ``/api/v1`` with a token of their own (RFC
``looper-client-skill`` §6; Settings → API clients).

Only the token's SHA-256 is stored, never the token. A revoked client keeps its row, so the threads, tasks and
Manager calls that came through it still name it.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable
from typing import Any

import asyncpg

from .connection import get_pool

TOKEN_PREFIX = "lpc_"
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")
# A client's last_used_at is moved at most this often.
TOUCH_SECONDS = 60

_VIEW_COLUMNS = "id, name, created_at, last_used_at, revoked_at"


class ClientNameError(ValueError):
    """The name is not a valid client name."""


class ClientNameTaken(ValueError):
    """An active client already has this name."""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def check_name(raw: Any) -> str:
    """The stripped name, or ``ClientNameError``."""
    name = raw.strip() if isinstance(raw, str) else ""
    if not NAME_PATTERN.fullmatch(name):
        raise ClientNameError("name: 1–40 characters: letters, digits, space, '.', '_' or '-', "
                              "starting with a letter or digit")
    return name


async def create(name: str, *, pool: asyncpg.Pool | None = None) -> tuple[dict[str, Any], str]:
    """Add a client; its view (never the hash) and its token, which is shown once and not stored."""
    name = check_name(name)
    pool = pool or get_pool()
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    client_id = "client-" + secrets.token_hex(6)
    try:
        row = await pool.fetchrow(
            f"INSERT INTO api_clients (id, name, token_sha256) VALUES ($1, $2, $3) RETURNING {_VIEW_COLUMNS}",
            client_id, name, token_hash(token))
    except asyncpg.UniqueViolationError as exc:
        if "idx_api_clients_active_name" not in (exc.constraint_name or ""):
            raise
        raise ClientNameTaken(f"an API client named {name!r} exists; revoke it or pick another name") from None
    return dict(row), token


async def find(token: str, *, pool: asyncpg.Pool | None = None) -> dict[str, Any] | None:
    """The active client this token belongs to (``id``, ``name``, ``last_used_at``), or None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT id, name, last_used_at FROM api_clients WHERE token_sha256 = $1 AND revoked_at IS NULL",
        token_hash(token))
    return dict(row) if row else None


async def touch(client_id: str, *, pool: asyncpg.Pool | None = None) -> None:
    """Note a use, at most once per ``TOUCH_SECONDS``."""
    pool = pool or get_pool()
    await pool.execute(
        "UPDATE api_clients SET last_used_at = now() WHERE id = $1 "
        "AND (last_used_at IS NULL OR last_used_at < now() - make_interval(secs => $2))",
        client_id, float(TOUCH_SECONDS))


async def list_active(*, pool: asyncpg.Pool | None = None) -> list[dict[str, Any]]:
    """The clients not revoked, newest first."""
    pool = pool or get_pool()
    rows = await pool.fetch(f"SELECT {_VIEW_COLUMNS} FROM api_clients WHERE revoked_at IS NULL "
                            "ORDER BY created_at DESC, id DESC")
    return [dict(row) for row in rows]


async def revoke(client_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Revoke a client; whether it was active."""
    pool = pool or get_pool()
    status = await pool.execute("UPDATE api_clients SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
                                client_id)
    return status.endswith(" 1")


async def names(ids: Iterable[str], *, pool: asyncpg.Pool | None = None) -> dict[str, str]:
    """Each id's client name, revoked clients included (for attribution)."""
    ids = list(ids)
    if not ids:
        return {}
    pool = pool or get_pool()
    rows = await pool.fetch("SELECT id, name FROM api_clients WHERE id = ANY($1::text[])", ids)
    return {row["id"]: row["name"] for row in rows}
