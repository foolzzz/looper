"""Legacy registry preflight and atomic application. No runtime discovery."""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from ..agents.registry_migration import LegacySnapshot, MigrationBlocked, plan, snapshot
from ..agents.profile_registry import MODEL_FIELDS, RuntimeSpec
from ..agents.runtimes import Capability
from .connection import _connection, get_pool

logger = logging.getLogger(__name__)
MIGRATION = 'llm_model_registry_v3'

# Historical effort migration must read and derive profiles before contract.
LEGACY_PROFILE_JOIN = ('agent_profiles a JOIN agent_runtimes r ON r.id = a.runtime_id '
                       'JOIN llm_providers p ON p.id = a.provider_id')
LEGACY_PROFILE_JSON = """json_build_object(
    'id', a.id, 'name', a.name, 'model', a.model, 'effort', a.effort,
    'agent_teams', a.agent_teams, 'custom_prompt', a.custom_prompt,
    'runtime_id', r.id, 'runtime_name', r.name, 'runtime_type', r.type,
    'runtime_executable', r.executable, 'runtime_capabilities', r.capabilities,
    'provider_id', p.id, 'provider_name', p.name, 'provider_auth', p.auth,
    'provider_protocols', p.protocols, 'provider_oauth_runtime_type', p.oauth_runtime_type,
    'provider_base_url', p.base_url, 'provider_models', p.models, 'provider_env', p.env)"""


async def insert_legacy_profile(fields: dict[str, Any], *, pool: asyncpg.Connection) -> str:
    from .profile_registry import new_id

    identity = new_id('agent')
    await pool.execute('INSERT INTO agent_profiles '
                       '(id, name, runtime_id, provider_id, model, effort, agent_teams, custom_prompt) '
                       'VALUES ($1, $2, $3, $4, $5, $6, $7, $8)', identity, fields['name'], fields['runtime_id'],
                       fields['provider_id'], fields['model'], fields['effort'], fields['agent_teams'],
                       fields['custom_prompt'])
    return identity


async def seed_legacy_builtin(conn: asyncpg.Connection) -> None:
    """Old model/layer migrations need the fixed Claude login before conversion."""
    await conn.execute("""
        INSERT INTO agent_runtimes (id, name, type, capabilities, builtin)
        VALUES ('rt-claude-code', 'claude-code', 'claude-code',
                ARRAY['add_dirs', 'autonomous_turns', 'claude_plugins', 'compaction_events', 'cost_report',
                      'mcp', 'resident_session', 'resume', 'run'], true) ON CONFLICT DO NOTHING;
        INSERT INTO llm_providers (id, name, auth, oauth_runtime_type, protocols, builtin)
        VALUES ('prov-claude-subscription', 'claude-subscription', 'oauth', 'claude-code',
                ARRAY['anthropic-messages'], true) ON CONFLICT DO NOTHING;
        INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, builtin, agent_teams)
        VALUES ('agent-claude-code-opus', 'claude-code/opus', 'rt-claude-code',
                'prov-claude-subscription', 'opus', true, true) ON CONFLICT DO NOTHING;
    """)


async def apply(conn: asyncpg.Connection) -> None:
    """Recompute and compare under ensure_schema's lock and transaction, then write.

    Only profile model references and session account identities change. The
    map is a non-secret historical snapshot; llm_records are never rewritten.
    """
    if not conn.is_in_transaction():
        raise ValueError('registry conversion requires the schema transaction')
    await conn.execute('LOCK TABLE agent_runtimes, llm_providers, llm_provider_secrets, agent_profiles, '
                       'agent_sessions, schedules IN SHARE ROW EXCLUSIVE MODE')
    result = plan(await _read_snapshot(conn))
    if not result.applicable:
        logger.error('registry conversion blocked: %s', json.dumps(result.report, sort_keys=True))
        raise MigrationBlocked('registry_conversion_blocked')
    converted = result.conversion
    assert converted is not None
    for provider in converted.providers.values():
        await conn.execute('UPDATE llm_providers SET vendor_type = $2 WHERE id = $1',
                           provider.id, provider.vendor_type.value)
    await conn.executemany('INSERT INTO llm_provider_endpoints '
                           '(provider_id, protocol, base_url, use_runtime_default) VALUES ($1, $2, $3, $4)',
                           [(e.provider_id, e.protocol.value, e.base_url, e.use_runtime_default)
                            for e in converted.endpoints.values()])
    fields = ('id', 'provider_id', 'protocol', *MODEL_FIELDS, 'catalog_order', 'builtin')
    await conn.executemany(f'INSERT INTO llm_models ({", ".join(fields)}) VALUES '
                           f'({", ".join(f"${i}" for i in range(1, len(fields) + 1))})',
                           [tuple(getattr(m, key) for key in fields) for m in converted.models.values()])
    await conn.executemany('INSERT INTO llm_model_migration_map '
                           '(legacy_provider_id, legacy_model, protocol, provider_id, model_id) '
                           'VALUES ($1, $2, $3, $4, $5)',
                           [(*key, *target) for key, target in converted.model_map.items()])
    # Update the old FK too before deleting merged accounts; contract drops it.
    await conn.executemany('UPDATE agent_profiles SET model_id = $2, provider_id = $3 WHERE id = $1',
                           [(p.id, p.model_id, converted.models[p.model_id].provider_id)
                            for p in converted.profiles.values()])
    for old, canonical in converted.provider_map.items():
        if old == canonical:
            continue
        await conn.execute('UPDATE agent_sessions SET provider_id = $2 WHERE provider_id = $1', old, canonical)
        await conn.execute('UPDATE schedules SET planner_session_provider = $2 WHERE planner_session_provider = $1',
                           old, canonical)
        # Canonical has the same verified saved key; the old secret cascades.
        await conn.execute('DELETE FROM llm_providers WHERE id = $1', old)
    logger.info('registry conversion verified: %s', json.dumps(result.report, sort_keys=True))


async def _read_snapshot(conn: asyncpg.Connection) -> LegacySnapshot:
    # Read raw rows: export's JOINs hide dangling profile references, and its
    # projection omits creation order needed to choose the canonical account.
    runtimes = [dict(r) for r in await conn.fetch("SELECT * FROM agent_runtimes ORDER BY id")]
    providers = [dict(r) for r in await conn.fetch(
        "SELECT p.*, s.api_key FROM llm_providers p LEFT JOIN llm_provider_secrets s ON s.provider_id = p.id "
        "ORDER BY p.id")]
    profiles = [dict(r) for r in await conn.fetch("SELECT * FROM agent_profiles ORDER BY id")]
    for row in providers:
        for key in ("models", "env"):
            if isinstance(row.get(key), str):
                row[key] = json.loads(row[key])
    sessions = []
    for table, column in (("agent_sessions", "provider_id"), ("schedules", "planner_session_provider")):
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass($1) "
                               "AND attname = $2 AND NOT attisdropped)", table, column):
            sessions.extend(r[column] for r in await conn.fetch(f"SELECT {column} FROM {table}"))
    return snapshot(
        [RuntimeSpec(r["id"], r["name"], r["type"], r.get("executable") or "",
                     frozenset(Capability(c) for c in r.get("capabilities") or ())) for r in runtimes],
        providers, profiles, session_provider_ids=sessions)


async def read_snapshot(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> LegacySnapshot:
    """Read a consistent private snapshot; existing transactions must be read-only
    and use repeatable-read or serializable isolation.

    A caller applying changes must take a new snapshot and recompute ``plan``
    in its own migration transaction, rather than reuse a dry-run result.
    """
    async with _connection(pool if pool is not None else get_pool()) as conn:
        if conn.is_in_transaction():
            if await conn.fetchval("SHOW transaction_read_only") != "on":
                raise ValueError("legacy preflight requires a read-only transaction")
            if await conn.fetchval("SHOW transaction_isolation") not in {"repeatable read", "serializable"}:
                raise ValueError("legacy preflight requires a repeatable-read snapshot")
            return await _read_snapshot(conn)
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            return await _read_snapshot(conn)


async def dry_run(pool: asyncpg.Pool | asyncpg.Connection | None = None) -> dict[str, Any]:
    """Return only the RFC 9.1 counts, never the snapshot or converted rows."""
    return plan(await read_snapshot(pool)).report
