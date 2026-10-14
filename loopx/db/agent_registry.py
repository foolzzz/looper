"""The ``agent_runtimes``, ``llm_providers`` (with ``llm_provider_secrets``) and ``agents`` tables.

Code owns the validation (``loopx.agents.agent_registry``); this module only
reads and writes rows. A provider's API key is read here only by
``get_api_key``; every other read says whether one is set.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import asyncpg

from .connection import get_pool

# The columns of an agent row joined with its runtime and provider, as one JSON object (also inside a layer).
AGENT_JSON = (
    "json_build_object('id', a.id, 'name', a.name, 'model', a.model, 'effort', a.effort, 'builtin', a.builtin, "
    "'runtime_id', r.id, 'runtime_name', r.name, 'runtime_type', r.type, 'runtime_executable', r.executable, "
    "'runtime_capabilities', r.capabilities, 'provider_id', p.id, 'provider_name', p.name, 'provider_auth', p.auth, "
    "'provider_protocols', p.protocols, 'provider_oauth_runtime_type', p.oauth_runtime_type, "
    "'provider_base_url', p.base_url, 'provider_models', p.models)"
)
AGENT_JOIN = "agents a JOIN agent_runtimes r ON r.id = a.runtime_id JOIN llm_providers p ON p.id = a.provider_id"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _provider(row: asyncpg.Record) -> dict[str, Any]:
    return {**dict(row), "models": _json(row["models"])}


# --- Runtimes ---


async def list_runtimes(*, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT r.*, (SELECT count(*) FROM agents a WHERE a.runtime_id = r.id) AS agent_count "
        "FROM agent_runtimes r ORDER BY r.builtin DESC, r.name"
    )
    return [dict(r) for r in rows]


async def get_runtime(runtime_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                      ) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow("SELECT * FROM agent_runtimes WHERE id = $1", runtime_id)
    return dict(row) if row else None


async def insert_runtime(fields: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> str:
    """Add a runtime (``name``, ``type``, ``executable``, ``version``, ``capabilities``, ``probe_error``); its id.

    Raises ``asyncpg.UniqueViolationError`` for a name in use.
    """
    pool = pool or get_pool()
    runtime_id = new_id("rt")
    await pool.execute(
        "INSERT INTO agent_runtimes (id, name, type, executable, version, capabilities, probed_at, probe_error) "
        "VALUES ($1, $2, $3, $4, $5, $6, now(), $7)",
        runtime_id, fields["name"], fields["type"], fields["executable"], fields["version"],
        list(fields["capabilities"]), fields["probe_error"],
    )
    return runtime_id


async def update_runtime(runtime_id: str, fields: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> bool:
    """Set ``name``, ``executable`` and the probe's ``version``, ``capabilities`` and ``probe_error`` (each
    optional); False when there is no such runtime."""
    pool = pool or get_pool()
    columns = [c for c in ("name", "executable", "version", "capabilities", "probe_error") if c in fields]
    sets = [f"{c} = ${i}" for i, c in enumerate(columns, start=2)]
    if "version" in fields:
        sets.append("probed_at = now()")
    values = [list(fields[c]) if c == "capabilities" else fields[c] for c in columns]
    result = await pool.execute(
        f"UPDATE agent_runtimes SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1", runtime_id, *values,
    )
    return result == "UPDATE 1"


async def delete_runtime(runtime_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Raises ``asyncpg.ForeignKeyViolationError`` while an agent uses it."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM agent_runtimes WHERE id = $1", runtime_id) == "DELETE 1"


# --- Providers ---


async def list_providers(*, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """Every provider, with ``api_key_set`` in place of its key."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT p.*, s.provider_id IS NOT NULL AS api_key_set, "
        "(SELECT count(*) FROM agents a WHERE a.provider_id = p.id) AS agent_count "
        "FROM llm_providers p LEFT JOIN llm_provider_secrets s ON s.provider_id = p.id "
        "ORDER BY p.builtin DESC, p.name"
    )
    return [_provider(r) for r in rows]


async def get_provider(provider_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                       ) -> dict[str, Any] | None:
    """A provider, with ``api_key_set`` in place of its key."""
    pool = pool or get_pool()
    row = await pool.fetchrow(
        "SELECT p.*, s.provider_id IS NOT NULL AS api_key_set FROM llm_providers p "
        "LEFT JOIN llm_provider_secrets s ON s.provider_id = p.id WHERE p.id = $1", provider_id,
    )
    return _provider(row) if row else None


async def get_api_key(provider_id: str, *, pool: asyncpg.Pool | None = None) -> str | None:
    """A provider's API key, for the launch that uses it; None without one."""
    pool = pool or get_pool()
    return await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1", provider_id)


_PROVIDER_COLUMNS = ("name", "protocols", "auth", "oauth_runtime_type", "base_url", "models", "max_concurrency")


def _provider_values(fields: Mapping[str, Any], columns: Sequence[str]) -> list[Any]:
    return [json.dumps(list(fields[c]), ensure_ascii=False) if c == "models" else
            list(fields[c]) if c == "protocols" else fields[c] for c in columns]


async def _set_secret(conn: asyncpg.Connection, provider_id: str, api_key: str | None) -> None:
    if api_key is None:
        await conn.execute("DELETE FROM llm_provider_secrets WHERE provider_id = $1", provider_id)
    else:
        await conn.execute(
            "INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES ($1, $2) "
            "ON CONFLICT (provider_id) DO UPDATE SET api_key = EXCLUDED.api_key", provider_id, api_key,
        )


async def insert_provider(conn: asyncpg.Connection, fields: Mapping[str, Any], api_key: str | None) -> str:
    """Add a provider (every column of ``_PROVIDER_COLUMNS``) and its key; its id. Raises
    ``asyncpg.UniqueViolationError`` for a name in use."""
    provider_id = new_id("prov")
    await conn.execute(
        f"INSERT INTO llm_providers (id, {', '.join(_PROVIDER_COLUMNS)}) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
        provider_id, *_provider_values(fields, _PROVIDER_COLUMNS),
    )
    if api_key is not None:
        await _set_secret(conn, provider_id, api_key)
    return provider_id


async def update_provider(conn: asyncpg.Connection, provider_id: str, fields: Mapping[str, Any], *,
                          api_key: str | None = None, keep_key: bool = True) -> None:
    """Set the columns in ``fields``; unless ``keep_key``, replace the key with ``api_key`` (None: remove it)."""
    columns = [c for c in _PROVIDER_COLUMNS if c in fields]
    sets = [f"{c} = ${i}" + ("::jsonb" if c == "models" else "") for i, c in enumerate(columns, start=2)]
    await conn.execute(
        f"UPDATE llm_providers SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
        provider_id, *_provider_values(fields, columns),
    )
    if not keep_key:
        await _set_secret(conn, provider_id, api_key)


async def delete_provider(provider_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Raises ``asyncpg.ForeignKeyViolationError`` while an agent uses it."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM llm_providers WHERE id = $1", provider_id) == "DELETE 1"


# --- Agents ---


async def list_agents(*, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                      provider_id: str | None = None) -> list[dict[str, Any]]:
    """Every agent (or those of one provider) as ``AGENT_JSON`` objects."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {AGENT_JSON} AS agent FROM {AGENT_JOIN} WHERE $1::text IS NULL OR a.provider_id = $1 "
        "ORDER BY a.builtin DESC, a.name", provider_id,
    )
    return [_json(r["agent"]) for r in rows]


async def get_agent(agent_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                    ) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchval(f"SELECT {AGENT_JSON} FROM {AGENT_JOIN} WHERE a.id = $1", agent_id)
    return _json(row) if row else None


async def insert_agent(fields: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> str:
    """Add an agent (``name``, ``runtime_id``, ``provider_id``, ``model``, ``effort``); its id. Raises
    ``asyncpg.UniqueViolationError`` for a name in use."""
    pool = pool or get_pool()
    agent_id = new_id("agent")
    await pool.execute(
        "INSERT INTO agents (id, name, runtime_id, provider_id, model, effort) VALUES ($1, $2, $3, $4, $5, $6)",
        agent_id, fields["name"], fields["runtime_id"], fields["provider_id"], fields["model"], fields["effort"],
    )
    return agent_id


async def update_agent(agent_id: str, fields: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> None:
    pool = pool or get_pool()
    columns = [c for c in ("name", "runtime_id", "provider_id", "model", "effort") if c in fields]
    sets = [f"{c} = ${i}" for i, c in enumerate(columns, start=2)]
    await pool.execute(f"UPDATE agents SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
                       agent_id, *[fields[c] for c in columns])


async def delete_agent(agent_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Raises ``asyncpg.ForeignKeyViolationError`` while a layer or a task selects it."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM agents WHERE id = $1", agent_id) == "DELETE 1"
