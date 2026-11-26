"""The compiled operator profile: one row, versioned only when the compiled text changes."""

from __future__ import annotations

import json
from typing import Any

from .connection import get_pool


def _body(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def put_source(key: str, path: str) -> None:
    pool = get_pool()
    await pool.execute(
        "INSERT INTO system_settings (key, value) VALUES ($1, $2) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        key, path)


async def clear_source(key: str) -> None:
    """Remove the folder and the compiled block in one transaction."""
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM system_settings WHERE key = $1", key)
        await conn.execute("DELETE FROM profile_blocks")


async def retain_or_replace(*, text: str, body: dict[str, Any]) -> int:
    """Refresh citations without advancing the version unless the compiled text changes."""
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True)
    pool = get_pool()
    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT version, text, body FROM profile_blocks WHERE id = TRUE FOR UPDATE")
        if row is not None and row["text"] == text and _body(row["body"]) == body:
            return int(row["version"])
        version = 1 if row is None else int(row["version"]) + int(row["text"] != text)
        await conn.execute(
            """
            INSERT INTO profile_blocks (id, version, text, body)
            VALUES (TRUE, $1, $2, $3::jsonb)
            ON CONFLICT (id) DO UPDATE SET
                version = EXCLUDED.version, text = EXCLUDED.text, body = EXCLUDED.body, compiled_at = now()
            """,
            version, text, encoded)
        return version
