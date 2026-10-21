"""The ``agent_runtimes``, ``llm_providers`` (with ``llm_provider_secrets``) and ``agent_profiles`` tables.

Runtimes are built in (``looper.agents.profile_registry``): the schema adds Claude Code's, ``looper serve`` and
Re-detect add another type's when its executable is on PATH, and a probe records what it found.

Code owns the validation (``looper.agents.profile_registry``); this module only
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

# Only account metadata is joined here; the launch path reads keys separately.
_PROVIDER_JSON = (
    "json_build_object('id', p.id, 'name', p.name, 'vendor_type', p.vendor_type, 'auth', p.auth, "
    "'oauth_runtime_type', p.oauth_runtime_type, 'env', p.env, 'max_concurrency', p.max_concurrency, "
    "'builtin', p.builtin)"
)
PROFILE_JSON = (
    "json_build_object('id', a.id, 'name', a.name, 'model_id', a.model_id, 'effort', a.effort, "
    "'builtin', a.builtin, 'agent_teams', a.agent_teams, 'custom_prompt', a.custom_prompt, "
    "'runtime_id', r.id, 'runtime_name', r.name, 'runtime_type', r.type, 'runtime_executable', r.executable, "
    "'runtime_capabilities', r.capabilities, 'provider_id', p.id, 'provider_name', p.name, "
    f"'provider', {_PROVIDER_JSON}, 'model', to_jsonb(m), 'endpoint', to_jsonb(e))"
)
PROFILE_JOIN = (
    "agent_profiles a JOIN agent_runtimes r ON r.id = a.runtime_id "
    "JOIN llm_models m ON m.id = a.model_id JOIN llm_providers p ON p.id = m.provider_id "
    "JOIN llm_provider_endpoints e ON e.provider_id = m.provider_id AND e.protocol = m.protocol"
)

# Correlated counts avoid multiplying profiles by the number of endpoints/models.
_PROVIDER_SELECT = (
    "SELECT p.*, EXISTS (SELECT 1 FROM llm_provider_secrets s WHERE s.provider_id = p.id) AS api_key_set, "
    "(SELECT count(*) FROM llm_models m WHERE m.provider_id = p.id) AS model_count, "
    "(SELECT count(*) FROM agent_profiles a JOIN llm_models m ON m.id = a.model_id "
    "WHERE m.provider_id = p.id) AS profile_count, "
    "COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.protocol) FROM llm_provider_endpoints e "
    "WHERE e.provider_id = p.id), '[]'::jsonb) AS endpoints FROM llm_providers p "
)
_MODEL_SELECT = (
    "SELECT m.*, p.name AS provider_name, "
    f"{_PROVIDER_JSON} AS provider, to_jsonb(e) AS endpoint, "
    "(SELECT count(*) FROM agent_profiles a WHERE a.model_id = m.id) AS profile_count "
    "FROM llm_models m JOIN llm_providers p ON p.id = m.provider_id "
    "JOIN llm_provider_endpoints e ON e.provider_id = m.provider_id AND e.protocol = m.protocol "
)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _provider(row: asyncpg.Record) -> dict[str, Any]:
    return {**dict(row), "endpoints": _json(row["endpoints"]), "env": _json(row["env"])}


# --- Runtimes ---


async def list_runtimes(*, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    pool = pool or get_pool()
    rows = await pool.fetch(
        "SELECT r.*, (SELECT count(*) FROM agent_profiles a WHERE a.runtime_id = r.id) AS profile_count "
        "FROM agent_runtimes r ORDER BY r.builtin DESC, r.name, r.id"
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
    rows = await pool.fetch(_PROVIDER_SELECT + "ORDER BY p.builtin DESC, p.name, p.id")
    return [_provider(r) for r in rows]


async def get_provider(provider_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                       ) -> dict[str, Any] | None:
    """A provider, with ``api_key_set`` in place of its key."""
    pool = pool or get_pool()
    row = await pool.fetchrow(_PROVIDER_SELECT + "WHERE p.id = $1", provider_id)
    return _provider(row) if row else None


def _model_row(row: asyncpg.Record) -> dict[str, Any]:
    return {**dict(row), "provider": _json(row["provider"]), "endpoint": _json(row["endpoint"])}


async def list_models(*, provider_id: str | None = None,
                      pool: asyncpg.Pool | asyncpg.Connection | None = None) -> list[dict[str, Any]]:
    """Models in saved catalog order, each with its exact account/endpoint and usage count."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        _MODEL_SELECT + "WHERE $1::text IS NULL OR m.provider_id = $1 "
        "ORDER BY p.builtin DESC, p.name, p.id, m.catalog_order, m.name, m.protocol, m.id", provider_id)
    return [_model_row(row) for row in rows]


async def get_model(model_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                    ) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchrow(_MODEL_SELECT + "WHERE m.id = $1", model_id)
    return _model_row(row) if row else None


async def get_api_key(provider_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> str | None:
    """A provider's API key, for the launch that uses it; None without one."""
    pool = pool or get_pool()
    return await pool.fetchval("SELECT api_key FROM llm_provider_secrets WHERE provider_id = $1", provider_id)


_PROVIDER_COLUMNS = ("name", "vendor_type", "auth", "oauth_runtime_type", "env", "max_concurrency")
_JSON_COLUMNS = ("env",)


def _provider_values(fields: Mapping[str, Any], columns: Sequence[str]) -> list[Any]:
    return [json.dumps(list(fields[c]), ensure_ascii=False) if c in _JSON_COLUMNS else fields[c] for c in columns]


async def lock_provider(conn: asyncpg.Connection, provider_id: str) -> bool:
    """Registry writes always lock account, then model, then profile."""
    return await conn.fetchval("SELECT id FROM llm_providers WHERE id = $1 FOR UPDATE", provider_id) is not None


async def lock_model(conn: asyncpg.Connection, model_id: str) -> dict[str, Any] | None:
    provider_id = await conn.fetchval("SELECT provider_id FROM llm_models WHERE id = $1", model_id)
    if provider_id is None or not await lock_provider(conn, provider_id):
        return None
    if not await conn.fetchval("SELECT id FROM llm_models WHERE id = $1 AND provider_id = $2 FOR UPDATE",
                               model_id, provider_id):
        return None
    return await get_model(model_id, pool=conn)


async def _set_endpoints(conn: asyncpg.Connection, provider_id: str, endpoints: Sequence[Mapping[str, Any]]) -> None:
    # Upsert retained endpoints: deleting and re-inserting would break model FKs.
    for endpoint in endpoints:
        await conn.execute(
            "INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url, use_runtime_default) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (provider_id, protocol) DO UPDATE SET "
            "base_url = EXCLUDED.base_url, use_runtime_default = CASE "
            "WHEN llm_provider_endpoints.base_url IS DISTINCT FROM EXCLUDED.base_url THEN false "
            "ELSE EXCLUDED.use_runtime_default END",
            provider_id, endpoint["protocol"], endpoint["base_url"], endpoint.get("use_runtime_default", False))
    await conn.execute("DELETE FROM llm_provider_endpoints WHERE provider_id = $1 "
                       "AND NOT (protocol = ANY($2::text[]))", provider_id, [e["protocol"] for e in endpoints])


async def _set_secret(conn: asyncpg.Connection, provider_id: str, api_key: str | None) -> None:
    if api_key is None:
        await conn.execute("DELETE FROM llm_provider_secrets WHERE provider_id = $1", provider_id)
    else:
        await conn.execute(
            "INSERT INTO llm_provider_secrets (provider_id, api_key) VALUES ($1, $2) "
            "ON CONFLICT (provider_id) DO UPDATE SET api_key = EXCLUDED.api_key", provider_id, api_key,
        )


async def insert_provider(conn: asyncpg.Connection, fields: Mapping[str, Any], api_key: str | None) -> str:
    """Add an account, its endpoints and separate key, without registering suggested models. Raises
    ``asyncpg.UniqueViolationError`` for a name in use."""
    provider_id = new_id("prov")
    fields = {"env": [], **fields}
    values = ", ".join(f"${i}" + ("::jsonb" if c in _JSON_COLUMNS else "")
                       for i, c in enumerate(_PROVIDER_COLUMNS, start=2))
    await conn.execute(
        f"INSERT INTO llm_providers (id, {', '.join(_PROVIDER_COLUMNS)}) VALUES ($1, {values})",
        provider_id, *_provider_values(fields, _PROVIDER_COLUMNS),
    )
    await _set_endpoints(conn, provider_id, fields.get("endpoints", []))
    if api_key is not None:
        await _set_secret(conn, provider_id, api_key)
    return provider_id


async def update_provider(conn: asyncpg.Connection, provider_id: str, fields: Mapping[str, Any], *,
                          api_key: str | None = None, keep_key: bool = True) -> None:
    """Set the columns in ``fields``; unless ``keep_key``, replace the key with ``api_key`` (None: remove it)."""
    await lock_provider(conn, provider_id)
    columns = [c for c in _PROVIDER_COLUMNS if c in fields]
    sets = [f"{c} = ${i}" + ("::jsonb" if c in _JSON_COLUMNS else "") for i, c in enumerate(columns, start=2)]
    await conn.execute(
        f"UPDATE llm_providers SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
        provider_id, *_provider_values(fields, columns),
    )
    if "endpoints" in fields:
        await _set_endpoints(conn, provider_id, fields["endpoints"])
    if not keep_key:
        await _set_secret(conn, provider_id, api_key)


async def delete_provider(provider_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    """Raises ``asyncpg.ForeignKeyViolationError`` while any model uses it, including unused models."""
    from .connection import _connection

    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        await lock_provider(conn, provider_id)
        return await conn.execute("DELETE FROM llm_providers WHERE id = $1", provider_id) == "DELETE 1"


async def insert_model(conn: asyncpg.Connection, fields: Mapping[str, Any]) -> str:
    """Insert a model under its account lock; the endpoint FK is never bypassed."""
    await lock_provider(conn, fields["provider_id"])
    model_id = f"model-{uuid.uuid4()}"
    columns = [c for c in ("provider_id", "name", "protocol", "context_window", "input_usd_per_mtok",
                           "output_usd_per_mtok", "cache_read_usd_per_mtok", "cache_write_usd_per_mtok",
                           "efforts", "background_model", "catalog_order") if c in fields]
    values = ", ".join(f"${i}" for i in range(2, len(columns) + 2))
    await conn.execute(f"INSERT INTO llm_models (id, {', '.join(columns)}) VALUES ($1, {values})",
                       model_id, *[fields[c] for c in columns])
    return model_id


async def update_model(conn: asyncpg.Connection, model_id: str, fields: Mapping[str, Any]) -> None:
    """Update settings only, retaining the immutable provider/name/protocol identity."""
    await lock_model(conn, model_id)
    columns = [c for c in ("context_window", "input_usd_per_mtok", "output_usd_per_mtok",
                           "cache_read_usd_per_mtok", "cache_write_usd_per_mtok", "efforts",
                           "background_model") if c in fields]
    sets = [f"{c} = ${i}" for i, c in enumerate(columns, start=2)]
    await conn.execute(f"UPDATE llm_models SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
                       model_id, *[fields[c] for c in columns])


async def delete_model(model_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> bool:
    """Delete only the live model; profile FKs restrict deletion, historical snapshots remain."""
    from .connection import _connection

    pool = pool or get_pool()
    async with _connection(pool) as conn, conn.transaction():
        await lock_model(conn, model_id)
        return await conn.execute("DELETE FROM llm_models WHERE id = $1", model_id) == "DELETE 1"


# --- Agent Profiles ---


async def list_profiles(*, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                        provider_id: str | None = None) -> list[dict[str, Any]]:
    """Every Agent Profile (or those of one provider) as ``PROFILE_JSON`` objects."""
    pool = pool or get_pool()
    rows = await pool.fetch(
        f"SELECT {PROFILE_JSON} AS profile FROM {PROFILE_JOIN} WHERE $1::text IS NULL OR p.id = $1 "
        "ORDER BY a.builtin DESC, a.name, a.id", provider_id,
    )
    return [_json(r["profile"]) for r in rows]


async def login_profile_count(runtime_type: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> int:
    """How many Agent Profiles use the own login (an OAuth provider) of ``runtime_type`` runtimes."""
    pool = pool or get_pool()
    return int(await pool.fetchval(
        f"SELECT count(*) FROM {PROFILE_JOIN} "
        "WHERE p.auth = 'oauth' AND p.oauth_runtime_type = $1", runtime_type))


async def get_profile(profile_id: str, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                      ) -> dict[str, Any] | None:
    pool = pool or get_pool()
    row = await pool.fetchval(f"SELECT {PROFILE_JSON} FROM {PROFILE_JOIN} WHERE a.id = $1", profile_id)
    return _json(row) if row else None


async def insert_profile(fields: Mapping[str, Any], *, pool: asyncpg.Pool | asyncpg.Connection | None = None) -> str:
    """Add an Agent Profile referencing an exact model, locking account before model. Raises
    ``asyncpg.UniqueViolationError`` for a name in use."""
    from .connection import _connection

    pool = pool or get_pool()
    # Profile ids keep the agent- prefix they had before the rename, so stored ids and new ones read alike.
    profile_id = new_id("agent")
    async with _connection(pool) as conn, conn.transaction():
        await lock_model(conn, fields["model_id"])
        await conn.execute(
            "INSERT INTO agent_profiles (id, name, runtime_id, model_id, effort, agent_teams, custom_prompt) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            profile_id, fields["name"], fields["runtime_id"], fields["model_id"], fields["effort"],
            fields["agent_teams"], fields.get("custom_prompt", ""))
    return profile_id


async def update_profile(profile_id: str, fields: Mapping[str, Any], *,
                         pool: asyncpg.Pool | asyncpg.Connection | None = None) -> None:
    pool = pool or get_pool()
    columns = [c for c in ("name", "runtime_id", "model_id", "effort", "agent_teams", "custom_prompt")
               if c in fields]
    sets = [f"{c} = ${i}" for i, c in enumerate(columns, start=2)]
    await pool.execute(f"UPDATE agent_profiles SET {', '.join([*sets, 'updated_at = now()'])} WHERE id = $1",
                       profile_id, *[fields[c] for c in columns])


async def profile_usage(profile_id: str | None = None, *, pool: asyncpg.Pool | asyncpg.Connection | None = None,
                        ) -> dict[str, dict[str, Any]]:
    """Where each Agent Profile (or ``profile_id``) is selected, by profile id: ``layers``, each ``{scope, role,
    project_id, project_name}`` (system, then roles, then projects by name), ``task_count``, how many tasks
    chose it for one of their roles, ``schedule_count``, how many schedules did, and ``allowed_in``, the names of
    the projects whose Planners may choose it (a Planner's choice counts only while its project allows it, so it is
    not counted), and ``manager_session``, whether the Manager's current session selects it. A profile nothing selects is not in the result."""
    pool = pool or get_pool()
    layers = await pool.fetch(
        "SELECT l.profile_id, l.scope, l.role, l.project_id, p.name AS project_name FROM agent_setting_layers l "
        "LEFT JOIN projects p ON p.id = l.project_id WHERE l.profile_id IS NOT NULL "
        "AND ($1::text IS NULL OR l.profile_id = $1) "
        "ORDER BY array_position(ARRAY['system', 'role', 'project', 'role_project'], l.scope), p.name, "
        "array_position(ARRAY['manager', 'planner', 'worker', 'reviewer'], l.role)", profile_id,
    )
    tasks = await pool.fetch(
        "SELECT profile_id, count(DISTINCT task_id) AS n FROM task_profile_overrides "
        "WHERE $1::text IS NULL OR profile_id = $1 GROUP BY profile_id", profile_id,
    )
    allowed = await pool.fetch(
        "SELECT l.profile_id, p.name FROM project_allowed_profiles l JOIN projects p ON p.id = l.project_id "
        "WHERE $1::text IS NULL OR l.profile_id = $1 ORDER BY p.name", profile_id,
    )
    schedules = await pool.fetch(
        "SELECT profile_id, count(DISTINCT schedule_id) AS n FROM schedule_profile_overrides "
        "WHERE $1::text IS NULL OR profile_id = $1 GROUP BY profile_id", profile_id,
    )
    manager = await pool.fetch(
        "SELECT selected_profile_id FROM agent_sessions WHERE agent_id = 'manager' "
        "AND selected_profile_id IS NOT NULL AND ($1::text IS NULL OR selected_profile_id = $1)", profile_id,
    )
    uses: dict[str, dict[str, Any]] = {}

    def use(profile: str) -> dict[str, Any]:
        return uses.setdefault(profile, {"layers": [], "task_count": 0, "schedule_count": 0, "allowed_in": [],
                                         "manager_session": False})

    for row in layers:
        use(row["profile_id"])["layers"].append({k: row[k] for k in ("scope", "role", "project_id", "project_name")})
    for row in tasks:
        use(row["profile_id"])["task_count"] = row["n"]
    for row in schedules:
        use(row["profile_id"])["schedule_count"] = row["n"]
    for row in allowed:
        use(row["profile_id"])["allowed_in"].append(row["name"])
    for row in manager:
        use(row["selected_profile_id"])["manager_session"] = True
    return uses


async def delete_profile_unchosen(profile_id: str, statuses: list[str], *, pool: asyncpg.Pool | None = None,
                                  ) -> list[dict[str, Any]]:
    """Delete ``profile_id`` unless a Planner of a task in one of ``statuses`` chose it: those tasks (``{id,
    title}``), and nothing deleted. The profile row is locked first, so a choice being written waits (its foreign
    key takes a share lock) and is seen, or fails once the profile is gone; it is never dropped by the cascade.
    Raises ``asyncpg.ForeignKeyViolationError`` while a layer or a task selects it."""
    from .agent_setting_layers import open_planner_choices

    pool = pool or get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.fetchval("SELECT id FROM agent_profiles WHERE id = $1 FOR UPDATE", profile_id)
        if chosen := await open_planner_choices(profile_id, statuses, pool=conn):
            return chosen
        await conn.execute("DELETE FROM agent_profiles WHERE id = $1", profile_id)
    return []


async def delete_profile(profile_id: str, *, pool: asyncpg.Pool | None = None) -> bool:
    """Raises ``asyncpg.ForeignKeyViolationError`` while a layer or a task selects it."""
    pool = pool or get_pool()
    return await pool.execute("DELETE FROM agent_profiles WHERE id = $1", profile_id) == "DELETE 1"
