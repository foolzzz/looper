"""The ``agent_runtimes``, ``llm_providers`` (with ``llm_provider_secrets``) and ``agents`` tables.

Runtimes are built in (``looper.agents.agent_registry``): the schema adds Claude Code's, ``looper serve`` and
Re-detect add another type's when its executable is on PATH, and a probe records what it found.

Code owns the validation (``looper.agents.agent_registry``); this module only
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
    "'provider_base_url', p.base_url, 'provider_models', p.models, 'provider_env', p.env)"
)
AGENT_JOIN = "agents a JOIN agent_runtimes r ON r.id = a.runtime_id JOIN llm_providers p ON p.id = a.provider_id"


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _provider(row: asyncpg.Record) -> dict[str, Any]:
    return {**dict(row), "models": _json(row["models"]), "env": _json(row["env"])}


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


async def insert_builtin_runtime(runtime_id: str, fields: Mapping[str, Any], *, pool: asyncpg.Pool | None = None,
                                 ) -> bool:
    """Add the built-in runtime ``runtime_id`` (``name``, ``type``, ``executable``, ``version``, ``capabilities``,
    ``probe_error``) unless it, or a runtime of its name, exists; whether it was added."""
    pool = pool or get_pool()
    result = await pool.execute(
        "INSERT INTO agent_runtimes (id, name, type, executable, version, capabilities, probed_at, probe_error, builtin) "
        "VALUES ($1, $2, $3, $4, $5, $6, now(), $7, true) ON CONFLICT DO NOTHING",
        runtime_id, fields["name"], fields["type"], fields["executable"], fields["version"],
        list(fields["capabilities"]), fields["probe_error"],
    )
    return result == "INSERT 0 1"


async def update_runtime(runtime_id: str, probe: Mapping[str, Any], *, pool: asyncpg.Pool | None = None) -> bool:
    """Record a probe's ``version``, ``capabilities`` and ``probe_error``; False when there is no such runtime."""
    pool = pool or get_pool()
    result = await pool.execute(
        "UPDATE agent_runtimes SET version = $2, capabilities = $3, probe_error = $4, probed_at = now(), "
        "updated_at = now() WHERE id = $1",
        runtime_id, probe["version"], list(probe["capabilities"]), probe["probe_error"],
    )
    return result == "UPDATE 1"


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


_PROVIDER_COLUMNS = ("name", "protocols", "auth", "oauth_runtime_type", "base_url", "models", "env",
                     "max_concurrency")
_JSON_COLUMNS = ("models", "env")


def _provider_values(fields: Mapping[str, Any], columns: Sequence[str]) -> list[Any]:
    return [json.dumps(list(fields[c]), ensure_ascii=False) if c in _JSON_COLUMNS else
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
    """Add a provider (every column of ``_PROVIDER_COLUMNS``; ``env`` optional) and its key; its id. Raises
    ``asyncpg.UniqueViolationError`` for a name in use."""
    provider_id = new_id("prov")
    fields = {"env": [], **fields}
    values = ", ".join(f"${i}" + ("::jsonb" if c in _JSON_COLUMNS else "")
                       for i, c in enumerate(_PROVIDER_COLUMNS, start=2))
    await conn.execute(
        f"INSERT INTO llm_providers (id, {', '.join(_PROVIDER_COLUMNS)}) VALUES ($1, {values})",
        provider_id, *_provider_values(fields, _PROVIDER_COLUMNS),
    )
    if api_key is not None:
        await _set_secret(conn, provider_id, api_key)
    return provider_id


async def update_provider(conn: asyncpg.Connection, provider_id: str, fields: Mapping[str, Any], *,
                          api_key: str | None = None, keep_key: bool = True) -> None:
    """Set the columns in ``fields``; unless ``keep_key``, replace the key with ``api_key`` (None: remove it)."""
    columns = [c for c in _PROVIDER_COLUMNS if c in fields]
    sets = [f"{c} = ${i}" + ("::jsonb" if c in _JSON_COLUMNS else "") for i, c in enumerate(columns, start=2)]
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


async def profile_usage(agent_id: str | None = None, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                     ) -> dict[str, dict[str, Any]]:
    """Where each agent (or ``agent_id``) is selected, by agent id: ``layers``, each ``{scope, role, project_id,
    project_name}`` (system, then roles, then projects by name), and ``task_count``, how many tasks chose it for
    one of their roles. An agent nothing selects is not in the result."""
    pool = pool or get_pool()
    layers = await pool.fetch(
        "SELECT l.agent_id, l.scope, l.role, l.project_id, p.name AS project_name FROM agent_setting_layers l "
        "LEFT JOIN projects p ON p.id = l.project_id WHERE l.agent_id IS NOT NULL "
        "AND ($1::text IS NULL OR l.agent_id = $1) "
        "ORDER BY array_position(ARRAY['system', 'role', 'project', 'role_project'], l.scope), p.name, "
        "array_position(ARRAY['manager', 'planner', 'worker', 'reviewer'], l.role)", agent_id,
    )
    tasks = await pool.fetch(
        "SELECT agent_id, count(DISTINCT task_id) AS n FROM task_agent_overrides "
        "WHERE $1::text IS NULL OR agent_id = $1 GROUP BY agent_id", agent_id,
    )
    uses: dict[str, dict[str, Any]] = {}
    for row in layers:
        entry = uses.setdefault(row["agent_id"], {"layers": [], "task_count": 0})
        entry["layers"].append({k: row[k] for k in ("scope", "role", "project_id", "project_name")})
    for row in tasks:
        uses.setdefault(row["agent_id"], {"layers": [], "task_count": 0})["task_count"] = row["n"]
    return uses


async def delete_agent(agent_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Raises ``asyncpg.ForeignKeyViolationError`` while a layer or a task selects it."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM agents WHERE id = $1", agent_id) == "DELETE 1"
