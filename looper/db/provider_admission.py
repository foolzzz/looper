"""Admission state and its append-only transition history."""

from contextlib import asynccontextmanager
from datetime import datetime

from .connection import get_pool


async def read_all(*, pool=None) -> list[dict]:
    pool = pool or get_pool()
    return [
        dict(row)
        for row in await pool.fetch(
            "SELECT * FROM provider_admission ORDER BY provider_id"
        )
    ]


@asynccontextmanager
async def _connection(pool):
    if hasattr(pool, "acquire"):
        async with pool.acquire() as conn:
            yield conn
    else:
        yield pool


async def save(
    provider_id: str,
    from_state: str,
    state: str,
    *,
    reason: str | None,
    until: datetime | None,
    until_known: bool,
    backoff_step: int,
    record_id: int | None,
    since: datetime,
    actor: str,
    pool=None,
) -> int:
    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        event_id = await conn.fetchval(
            "INSERT INTO provider_admission_events(provider_id,from_state,to_state,reason,until,record_id,actor) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id",
            provider_id,
            from_state,
            state,
            reason,
            until,
            record_id,
            actor,
        )
        await conn.execute(
            "INSERT INTO provider_admission(provider_id,state,reason,until,until_known,backoff_step,record_id,since) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(provider_id) DO UPDATE SET "
            "state=EXCLUDED.state,reason=EXCLUDED.reason,until=EXCLUDED.until,until_known=EXCLUDED.until_known,"
            "backoff_step=EXCLUDED.backoff_step,record_id=EXCLUDED.record_id,since=EXCLUDED.since,updated_at=now()",
            provider_id,
            state,
            reason,
            until,
            until_known,
            backoff_step,
            record_id,
            since,
        )
    return event_id


async def prune(*, pool=None) -> int:
    result = await (pool or get_pool()).execute(
        "DELETE FROM provider_admission_events WHERE created_at < now() - interval '90 days'"
    )
    return int(result.split()[-1])
