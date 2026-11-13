"""Local gateway aliases and their exact model/account targets."""

from __future__ import annotations

import re

from .connection import get_pool

ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_TARGET = """SELECT m.id FROM llm_models m JOIN llm_providers p ON p.id=m.provider_id
    JOIN llm_provider_secrets s ON s.provider_id=p.id
    WHERE m.id=$2 AND m.protocol='openai-chat' AND p.auth='api_key' AND p.enabled AND s.api_key<>''"""
_TARGET_ERROR = "target must be an openai-chat model on an enabled API-key provider with a saved key"


def check_alias(alias):
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
        raise ValueError(
            "alias must contain 1–64 letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit"
        )


async def list_aliases(*, pool=None):
    rows = await (pool if pool is not None else get_pool()).fetch("""SELECT a.alias,a.model_id,m.name AS model_name,
        p.id AS provider_id,p.name AS provider_name,p.enabled FROM gateway_aliases a
        JOIN llm_models m ON m.id=a.model_id JOIN llm_providers p ON p.id=m.provider_id ORDER BY a.alias""")
    return [dict(row) for row in rows]


async def set_alias(alias, model_id, *, pool=None):
    check_alias(alias)
    pool = pool if pool is not None else get_pool()
    # Validate in the write itself so a concurrent account change cannot slip between reads.
    result = await pool.execute(
        f"""INSERT INTO gateway_aliases(alias,model_id)
        SELECT $1,target.id FROM ({_TARGET}) target
        ON CONFLICT(alias) DO UPDATE SET model_id=EXCLUDED.model_id,updated_at=now()""",
        alias,
        model_id,
    )
    if result == "INSERT 0 0":
        raise ValueError(_TARGET_ERROR)


async def check_binding(alias, model_id, *, pool=None):
    """Read-only confirmation preflight; set_alias rechecks the target atomically when writing."""
    check_alias(alias)
    target = _TARGET.replace("$2", "$1")
    if not await (pool if pool is not None else get_pool()).fetchval(f"SELECT EXISTS({target})", model_id):
        raise ValueError(_TARGET_ERROR)


async def delete_alias(alias, *, pool=None):
    await (pool if pool is not None else get_pool()).execute("DELETE FROM gateway_aliases WHERE alias=$1", alias)


async def resolve_alias(alias, *, pool=None):
    # Keep disabled accounts visible to the caller, which returns permission_error rather than not_found.
    row = await (pool if pool is not None else get_pool()).fetchrow(
        """SELECT e.base_url,m.name AS upstream_model,
        m.chat_dialect,p.vendor_type,p.id AS provider_id,s.api_key,p.enabled,p.auth,m.protocol
        FROM gateway_aliases a JOIN llm_models m ON m.id=a.model_id
        JOIN llm_provider_endpoints e ON e.provider_id=m.provider_id AND e.protocol=m.protocol
        JOIN llm_providers p ON p.id=m.provider_id
        LEFT JOIN llm_provider_secrets s ON s.provider_id=p.id
        WHERE a.alias=$1 AND m.protocol='openai-chat' """,
        alias,
    )
    return dict(row) if row else None
