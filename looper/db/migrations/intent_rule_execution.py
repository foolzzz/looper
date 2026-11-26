"""Recoverable rule work, shared MR revision ownership and execution ledgers."""
from __future__ import annotations

name = "intent_rule_execution"
order = pending_order = 1900

_REASONS = (
    "matched", "no_match", "type_mismatch", "scope_mismatch", "condition_failed", "flagged",
    "ambiguous_target", "verification_unavailable", "adapter_missing", "adapter_start_failed",
    "verification_timeout", "verification_output_limit", "verification_exit", "verification_protocol",
    "authentication_failed", "permission_denied", "not_readable", "cannot_decide", "upstream_error",
    "mr_missing", "mr_not_open", "repository_denied", "not_reviewer", "not_addressed", "sender_denied",
    "config_changed", "project_changed", "template_changed", "source_revoked", "switch_off", "delivery_off",
    "intents_off", "revision_changed", "deduped", "unknown_legacy_revision", "daily_cap", "audit_unavailable",
    "start_failed", "notification_failed", "revoke_failed", "already_terminal",
)
_REASON_SQL = "(" + ",".join("'" + value + "'" for value in _REASONS) + ")"
_TABLES = {
    "intent_rule_discoveries": "id discovery_source_id source_ref system_id mr_url state task_id created_at handled_at",
    "intent_rule_work": "id intent_id discovery_id generation source_fingerprint prepared_target first_fact "
        "first_verified_at first_verification_status first_reason state lease_token lease_until decision reason "
        "hit_id created_at decided_at",
    "intent_rule_mr_targets": "system_id repository_id mr_id blocked_reason",
    "intent_rule_mr_claims": "system_id repository_id mr_id revision task_id created_at",
    "intent_rule_project_days": "operator_key project_id local_day used timezone updated_at",
    "intent_rule_executions": "id task_id mode mr_key quota_day quota_operator quota_project quota_units start_state "
        "revoke_state last_error_code created_at updated_at",
    "intent_rule_hit_executions": "hit_id execution_id",
    "intent_rule_task_starts": "task_id execution_id state lease_token lease_until attempts next_attempt_at "
        "last_error_code created_at updated_at",
    "intent_rule_notices": "id execution_id discovery_id kind dedupe_key state channel_id chat external_id attempts "
        "decision reason last_error_code created_at updated_at",
    "intent_rule_verification_health": "system_id consecutive_unavailable episode alerted created_at updated_at",
    "intent_rule_hits": "attempts",
    "intent_rule_migration_previews": "id operator_key generation discovery_source_id entries fingerprint expires_at "
        "consumed_at result created_at",
}
_CONSTRAINTS = {
    "intent_rule_discoveries": {
        "origin_key": "UNIQUE (discovery_source_id,source_ref)",
        "state_check": "CHECK (state IN ('pending','confirmation','tasked','recorded','rejected'))",
        "task_id_fkey": "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL",
    },
    "intent_rule_work": {
        "intent_id_key": "UNIQUE (intent_id)", "discovery_id_key": "UNIQUE (discovery_id)",
        "intent_id_fkey": "FOREIGN KEY (intent_id) REFERENCES intents(id) ON DELETE CASCADE",
        "discovery_id_fkey": "FOREIGN KEY (discovery_id) REFERENCES intent_rule_discoveries(id) ON DELETE CASCADE",
        "hit_id_fkey": "FOREIGN KEY (hit_id) REFERENCES intent_rule_hits(id) ON DELETE SET NULL",
        "source_check": "CHECK ((intent_id IS NULL) <> (discovery_id IS NULL))",
        "generation_check": "CHECK (generation >= 0)",
        "prepared_check": "CHECK (prepared_target IS NULL OR jsonb_typeof(prepared_target)='object')",
        "fact_check": "CHECK (first_fact IS NULL OR jsonb_typeof(first_fact)='object')",
        "verification_check": "CHECK (first_verification_status IN ('verified','unavailable'))",
        "first_reason_check": f"CHECK (first_reason IS NULL OR first_reason IN {_REASON_SQL})",
        "state_check": "CHECK (state IN ('pending','evaluating','decided'))",
        "lease_check": "CHECK ((lease_token IS NULL) = (lease_until IS NULL))",
        "decision_check": "CHECK (decision IS NULL OR decision IN ('push_for_confirmation','create_task','record_only'))",
        "reason_check": f"CHECK (reason IS NULL OR reason IN {_REASON_SQL})",
    },
    "intent_rule_mr_targets": {
        "blocked_check": "CHECK (blocked_reason IS NULL OR blocked_reason='unknown_legacy_revision')",
    },
    "intent_rule_mr_claims": {
        "target_fkey": "FOREIGN KEY (system_id,repository_id,mr_id) REFERENCES "
                       "intent_rule_mr_targets(system_id,repository_id,mr_id)",
    },
    "intent_rule_project_days": {"used_check": "CHECK (used >= 0)"},
    "intent_rule_executions": {
        "task_id_key": "UNIQUE (task_id)",
        "mode_check": "CHECK (mode IN ('auto','manual'))",
        "key_check": "CHECK (jsonb_typeof(mr_key)='object')",
        "units_check": "CHECK (quota_units IN (0,1))",
        "quota_check": "CHECK ((quota_units=0 AND quota_day IS NULL AND quota_operator IS NULL AND quota_project IS NULL) "
                       "OR (quota_units=1 AND mode='auto' AND quota_day IS NOT NULL AND quota_operator IS NOT NULL "
                       "AND quota_project IS NOT NULL))",
        "start_check": "CHECK (start_state IN ('pending','requested','started','failed','cancelled'))",
        "revoke_check": "CHECK (revoke_state IN ('none','requested','stopped','already_terminal','failed'))",
        "error_check": f"CHECK (last_error_code IS NULL OR last_error_code IN {_REASON_SQL})",
    },
    "intent_rule_hit_executions": {
        "hit_id_fkey": "FOREIGN KEY (hit_id) REFERENCES intent_rule_hits(id) ON DELETE CASCADE",
        "execution_id_fkey": "FOREIGN KEY (execution_id) REFERENCES intent_rule_executions(id)",
    },
    "intent_rule_task_starts": {
        "execution_id_key": "UNIQUE (execution_id)",
        "execution_id_fkey": "FOREIGN KEY (execution_id) REFERENCES intent_rule_executions(id)",
        "state_check": "CHECK (state IN ('pending','claimed','acknowledged','cancelled'))",
        "lease_check": "CHECK ((lease_token IS NULL) = (lease_until IS NULL))",
        "attempts_check": "CHECK (attempts >= 0)",
        "error_check": f"CHECK (last_error_code IS NULL OR last_error_code IN {_REASON_SQL})",
    },
    "intent_rule_notices": {
        "execution_id_fkey": "FOREIGN KEY (execution_id) REFERENCES intent_rule_executions(id)",
        "discovery_id_fkey": "FOREIGN KEY (discovery_id) REFERENCES intent_rule_discoveries(id) ON DELETE CASCADE",
        "dedupe_key_key": "UNIQUE (dedupe_key)",
        "kind_check": "CHECK (kind IN ('created','discovery_confirmation','verification_fault'))",
        "source_check": "CHECK ((kind='created' AND execution_id IS NOT NULL AND discovery_id IS NULL) "
                        "OR (kind='discovery_confirmation' AND discovery_id IS NOT NULL AND execution_id IS NULL) "
                        "OR (kind='verification_fault' AND execution_id IS NULL AND discovery_id IS NULL))",
        "state_check": "CHECK (state IN ('pending','sending','sent','digest','failed','uncertain'))",
        "attempts_check": "CHECK (attempts >= 0)",
        "decision_check": "CHECK (decision IS NULL OR decision IN ('send','digest','drop'))",
        "reason_check": f"CHECK (reason IS NULL OR reason IN {_REASON_SQL} OR reason IN ('within_limits','recent_reply','quiet_hours','outside_active_hours','silent','urgent'))",
        "error_check": f"CHECK (last_error_code IS NULL OR last_error_code IN {_REASON_SQL})",
    },
    "intent_rule_verification_health": {
        "unavailable_check": "CHECK (consecutive_unavailable >= 0)",
        "episode_check": "CHECK (episode >= 0)",
    },
    "intent_rule_migration_previews": {
        "generation_check": "CHECK (generation >= 0)",
        "entries_check": "CHECK (jsonb_typeof(entries)='array' AND jsonb_array_length(entries) BETWEEN 1 AND 100)",
        "result_check": "CHECK (result IS NULL OR jsonb_typeof(result)='object')",
        "consumed_check": "CHECK ((consumed_at IS NULL) = (result IS NULL))",
    },
    "intent_rule_hits": {
        "attempts_check": "CHECK (jsonb_typeof(attempts)='array')",
        "selection_check": "CHECK (selection IN ('matched','unmatched','fallback','manual'))",
        "discovery_id_fkey": "FOREIGN KEY (discovery_id) REFERENCES intent_rule_discoveries(id) ON DELETE CASCADE",
    },
}
_KEYS = {"intent_rule_mr_targets": "system_id,repository_id,mr_id", "intent_rule_mr_claims": "system_id,repository_id,mr_id,revision",
         "intent_rule_project_days": "operator_key,project_id,local_day", "intent_rule_hit_executions": "hit_id",
         "intent_rule_task_starts": "task_id", "intent_rule_verification_health": "system_id"}
for _table in _TABLES:
    _CONSTRAINTS[_table]["pkey"] = "PRIMARY KEY (" + _KEYS.get(_table, "id") + ")"

fresh_ddl = """
ALTER TABLE intent_rule_hits ADD COLUMN IF NOT EXISTS attempts JSONB NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS intent_rule_discoveries (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 discovery_source_id TEXT NOT NULL, source_ref TEXT NOT NULL, system_id TEXT NOT NULL, mr_url TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', task_id TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), handled_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS intent_rule_work (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 intent_id BIGINT, discovery_id BIGINT, generation BIGINT NOT NULL, source_fingerprint TEXT NOT NULL,
 prepared_target JSONB, first_fact JSONB, first_verified_at TIMESTAMPTZ,
 first_verification_status TEXT NOT NULL, first_reason TEXT,
 state TEXT NOT NULL DEFAULT 'pending', lease_token UUID, lease_until TIMESTAMPTZ,
 decision TEXT, reason TEXT, hit_id BIGINT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), decided_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS intent_rule_mr_targets (
 system_id TEXT NOT NULL, repository_id TEXT NOT NULL, mr_id TEXT NOT NULL, blocked_reason TEXT,
 PRIMARY KEY(system_id,repository_id,mr_id)
);
CREATE TABLE IF NOT EXISTS intent_rule_mr_claims (
 system_id TEXT NOT NULL, repository_id TEXT NOT NULL, mr_id TEXT NOT NULL, revision TEXT NOT NULL,
 task_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(system_id,repository_id,mr_id,revision)
);
CREATE TABLE IF NOT EXISTS intent_rule_project_days (
 operator_key TEXT NOT NULL, project_id TEXT NOT NULL, local_day DATE NOT NULL,
 used INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(operator_key,project_id,local_day)
);
CREATE TABLE IF NOT EXISTS intent_rule_executions (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 task_id TEXT NOT NULL, mode TEXT NOT NULL, mr_key JSONB NOT NULL,
 quota_day DATE, quota_operator TEXT, quota_project TEXT, quota_units INTEGER NOT NULL DEFAULT 0,
 start_state TEXT NOT NULL DEFAULT 'pending', revoke_state TEXT NOT NULL DEFAULT 'none', last_error_code TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_hit_executions (
 hit_id BIGINT PRIMARY KEY, execution_id BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS intent_rule_task_starts (
 task_id TEXT PRIMARY KEY, execution_id BIGINT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 lease_token UUID, lease_until TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_error_code TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_notices (
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 execution_id BIGINT, discovery_id BIGINT, kind TEXT NOT NULL, dedupe_key TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', channel_id TEXT, chat TEXT, external_id TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, decision TEXT, reason TEXT, last_error_code TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_verification_health (
 system_id TEXT PRIMARY KEY, consecutive_unavailable INTEGER NOT NULL DEFAULT 0,
 episode BIGINT NOT NULL DEFAULT 0, alerted BOOLEAN NOT NULL DEFAULT false,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_migration_previews (
 id UUID PRIMARY KEY, operator_key TEXT NOT NULL, generation BIGINT NOT NULL, discovery_source_id TEXT NOT NULL,
 entries JSONB NOT NULL, fingerprint TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
 consumed_at TIMESTAMPTZ, result JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""
# Repair missing constraints on replay, including evidence FKs removed by an earlier table restore.
for _table, _constraints in _CONSTRAINTS.items():
    for _suffix, _definition in _constraints.items():
        _name = f"{_table}_{_suffix}"
        if _table == "intent_rule_hits" and _suffix == "selection_check":
            fresh_ddl += f"ALTER TABLE {_table} DROP CONSTRAINT IF EXISTS {_name};\n"
        fresh_ddl += f"""
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='{_table}'::regclass AND conname='{_name}') THEN
  ALTER TABLE {_table} ADD CONSTRAINT {_name} {_definition};
 END IF;
END $$;
"""
_INDEXES = {
    "idx_intent_rule_work_ready": "ON intent_rule_work(state,lease_until,id) WHERE state <> 'decided'",
    "idx_intent_rule_starts_ready": "ON intent_rule_task_starts(state,next_attempt_at,lease_until)",
    "idx_intent_rule_notices_ready": "ON intent_rule_notices(state,id)",
    "idx_intent_rule_notices_created": "UNIQUE ON intent_rule_notices(execution_id) WHERE kind='created'",
    "idx_intent_rule_hit_execution": "ON intent_rule_hit_executions(execution_id)",
    "idx_intent_rule_claims_task": "ON intent_rule_mr_claims(task_id)",
    "idx_intent_rule_execution_created": "ON intent_rule_executions(created_at,id)",
}
for _index, _definition in _INDEXES.items():
    _unique = "UNIQUE " if _definition.startswith("UNIQUE ") else ""
    fresh_ddl += f"CREATE {_unique}INDEX IF NOT EXISTS {_index} {_definition.removeprefix('UNIQUE ')};\n"


async def pending(conn):
    """Check actual objects even when the marker survived an incomplete restore."""
    for table, columns in _TABLES.items():
        found = await conn.fetch("SELECT attname FROM pg_attribute WHERE attrelid=to_regclass($1) "
                                 "AND attnum>0 AND NOT attisdropped", table)
        if not set(columns.split()).issubset({row["attname"] for row in found}):
            return True
    for table, constraints in _CONSTRAINTS.items():
        found = await conn.fetch("SELECT conname FROM pg_constraint WHERE conrelid=to_regclass($1)", table)
        if not {f"{table}_{suffix}" for suffix in constraints}.issubset({row["conname"] for row in found}):
            return True
    definition = await conn.fetchval("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid=to_regclass('intent_rule_hits') AND conname='intent_rule_hits_selection_check'")
    if not definition or "'manual'" not in definition:
        return True
    return not await conn.fetchval("SELECT count(*)=$1 FROM pg_index WHERE indisvalid AND indexrelid = ANY("
                                   "SELECT to_regclass(unnest($2::text[])))", len(_INDEXES), list(_INDEXES))


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    if await pending(conn):
        did_work = True
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
    return did_work
