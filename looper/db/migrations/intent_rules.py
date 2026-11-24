"""Versioned operator rules and configuration previews; no runtime activation."""

name = "intent_rules"
order = 1880
pending_order = 1880

_TABLES = {
    "intent_rule_config": "id generation adapter discovery_sources activation_acceptance updated_at",
    "intent_rules": "id version position deleted_at",
    "intent_rule_versions": "rule_id version enabled position intent_type scope conditions action project_id template_key "
                            "rolled_back_from edited_by created_at",
    "intent_rule_previews": "id operator_key expected_generation command reference_fingerprints diff expires_at "
                            "consumed_at result created_at",
    "intent_rule_config_audit": "id generation preview_id operator_key change created_at",
    "intent_rule_template_contracts": "workflow_key version workflow_digest input_kind comment_mode "
                                     "approve_requires_confirmation withdraw_requires_confirmation allow_merge "
                                     "allow_reviewer_changes updated_at",
    "intent_rule_hits": "id evaluation_key intent_id discovery_id generation rule_id rule_version selection action "
                        "reason verified_at mr_identity created_at",
    "intent_rule_condition_results": "hit_id rule_position condition_position rule_id rule_version kind result reason "
                                     "verified_at",
    "intent_rule_hit_sources": "hit_id event_seq",
    "projects": "auto_task_daily_cap auto_task_config_version",
}
_ACTIONS = "('push_for_confirmation','create_task','record_only')"
_CONSTRAINTS = {
    "intent_rule_config": {
        "intent_rule_config_singleton": "CHECK (id)",
        "intent_rule_config_generation_check": "CHECK (generation >= 0)",
        "intent_rule_config_adapter_check": "CHECK (adapter IS NULL OR jsonb_typeof(adapter) = 'object')",
        "intent_rule_config_sources_check": "CHECK (jsonb_typeof(discovery_sources) = 'array')",
        "intent_rule_config_acceptance_check": "CHECK (activation_acceptance IS NULL OR "
                                               "jsonb_typeof(activation_acceptance) = 'array')",
    },
    "intent_rules": {
        "intent_rules_version_check": "CHECK (version >= 1)",
        "intent_rules_position_check": "CHECK ((deleted_at IS NULL AND position IS NOT NULL AND position > 0) "
                                       "OR (deleted_at IS NOT NULL AND position IS NULL))",
        "intent_rules_position_key": "UNIQUE (position) DEFERRABLE INITIALLY DEFERRED",
    },
    "intent_rule_versions": {
        "intent_rule_versions_rule_id_fkey": "FOREIGN KEY (rule_id) REFERENCES intent_rules(id) ON DELETE CASCADE",
        "intent_rule_versions_version_check": "CHECK (version >= 1)",
        "intent_rule_versions_position_check": "CHECK (position > 0)",
        "intent_rule_versions_scope_check": "CHECK (jsonb_typeof(scope) = 'object')",
        "intent_rule_versions_conditions_check": "CHECK (jsonb_typeof(conditions) = 'array')",
        "intent_rule_versions_action_check": f"CHECK (action IN {_ACTIONS})",
        "intent_rule_versions_rollback_check": "CHECK (rolled_back_from IS NULL OR rolled_back_from >= 1)",
    },
    "intent_rule_previews": {
        "intent_rule_previews_generation_check": "CHECK (expected_generation >= 0)",
        "intent_rule_previews_command_check": "CHECK (jsonb_typeof(command) = 'object')",
        "intent_rule_previews_fingerprints_check": "CHECK (jsonb_typeof(reference_fingerprints) = 'object')",
        "intent_rule_previews_diff_check": "CHECK (jsonb_typeof(diff) = 'object')",
        "intent_rule_previews_result_check": "CHECK (result IS NULL OR jsonb_typeof(result) = 'object')",
        "intent_rule_previews_consumed_check": "CHECK ((consumed_at IS NULL) = (result IS NULL))",
    },
    "intent_rule_config_audit": {
        "intent_rule_config_audit_generation_check": "CHECK (generation > 0)",
        "intent_rule_config_audit_preview_id_key": "UNIQUE (preview_id)",
        "intent_rule_config_audit_change_check": "CHECK (jsonb_typeof(change) = 'object')",
    },
    "intent_rule_template_contracts": {
        "intent_rule_template_contracts_workflow_key_fkey": "FOREIGN KEY (workflow_key) REFERENCES workflows(key) "
                                                            "ON DELETE CASCADE",
        "intent_rule_template_contracts_version_check": "CHECK (version >= 1)",
        "intent_rule_template_contracts_input_check": "CHECK (input_kind = 'verified_mr')",
        "intent_rule_template_contracts_comment_check": "CHECK (comment_mode IN ('direct','approval_gate'))",
        "intent_rule_template_contracts_approve_check": "CHECK (approve_requires_confirmation)",
        "intent_rule_template_contracts_withdraw_check": "CHECK (withdraw_requires_confirmation)",
        "intent_rule_template_contracts_merge_check": "CHECK (NOT allow_merge)",
        "intent_rule_template_contracts_reviewers_check": "CHECK (NOT allow_reviewer_changes)",
    },
    "intent_rule_hits": {
        "intent_rule_hits_evaluation_key_key": "UNIQUE (evaluation_key)",
        "intent_rule_hits_intent_id_fkey": "FOREIGN KEY (intent_id) REFERENCES intents(id) ON DELETE CASCADE",
        "intent_rule_hits_source_check": "CHECK ((intent_id IS NULL) <> (discovery_id IS NULL))",
        "intent_rule_hits_generation_check": "CHECK (generation >= 0)",
        "intent_rule_hits_rule_check": "CHECK ((rule_id IS NULL) = (rule_version IS NULL))",
        "intent_rule_hits_version_check": "CHECK (rule_version IS NULL OR rule_version >= 1)",
        "intent_rule_hits_selection_check": "CHECK (selection IN ('matched','unmatched','fallback'))",
        "intent_rule_hits_action_check": f"CHECK (action IN {_ACTIONS})",
        "intent_rule_hits_identity_check": "CHECK (mr_identity IS NULL OR jsonb_typeof(mr_identity) = 'object')",
    },
    "intent_rule_condition_results": {
        "intent_rule_condition_results_hit_id_fkey": "FOREIGN KEY (hit_id) REFERENCES intent_rule_hits(id) "
                                                    "ON DELETE CASCADE",
        "intent_rule_condition_results_position_check": "CHECK (rule_position > 0 AND condition_position > 0)",
        "intent_rule_condition_results_version_check": "CHECK (rule_version >= 1)",
        "intent_rule_condition_results_kind_check": "CHECK (kind IN ('internal_mr_open','operator_is_reviewer',"
                                                    "'addressed_to_operator','sender_in_allowlist',"
                                                    "'mr_repository_in_allowlist'))",
        "intent_rule_condition_results_result_check": "CHECK (result IN ('passed','failed','unavailable'))",
        "intent_rule_condition_results_reason_check": "CHECK (reason IN ('passed','condition_not_met',"
            "'verification_unavailable','authentication_failed','permission_denied','not_readable','cannot_decide',"
            "'upstream_error','invalid_response','adapter_not_configured','mr_not_found','mr_not_open',"
            "'reviewer_missing','not_addressed','sender_not_allowed','repository_not_allowed','scope_mismatch',"
            "'type_mismatch','ambiguous_target'))",
    },
    "intent_rule_hit_sources": {
        "intent_rule_hit_sources_hit_id_fkey": "FOREIGN KEY (hit_id) REFERENCES intent_rule_hits(id) ON DELETE CASCADE",
        "intent_rule_hit_sources_event_seq_fkey": "FOREIGN KEY (event_seq) REFERENCES source_events(seq) ON DELETE CASCADE",
    },
    "projects": {
        "projects_auto_task_daily_cap_check": "CHECK (auto_task_daily_cap IS NULL OR auto_task_daily_cap > 0)",
        "projects_auto_task_config_version_check": "CHECK (auto_task_config_version >= 0)",
    },
}
for _table in _TABLES:
    if _table == "projects":
        continue
    _key = {"intent_rule_versions": "rule_id, version",
            "intent_rule_template_contracts": "workflow_key",
            "intent_rule_condition_results": "hit_id, rule_position, condition_position",
            "intent_rule_hit_sources": "hit_id, event_seq"}.get(_table, "id")
    _CONSTRAINTS[_table][f"{_table}_pkey"] = f"PRIMARY KEY ({_key})"

fresh_ddl = """
CREATE TABLE IF NOT EXISTS intent_rule_config (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE,
    generation BIGINT NOT NULL DEFAULT 0,
    adapter JSONB,
    discovery_sources JSONB NOT NULL DEFAULT '[]',
    activation_acceptance JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rules (
    id UUID PRIMARY KEY,
    version INTEGER NOT NULL,
    position INTEGER,
    deleted_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS intent_rule_versions (
    rule_id UUID NOT NULL,
    version INTEGER NOT NULL,
    enabled BOOLEAN NOT NULL,
    position INTEGER NOT NULL,
    intent_type TEXT NOT NULL,
    scope JSONB NOT NULL,
    conditions JSONB NOT NULL,
    action TEXT NOT NULL,
    project_id TEXT,
    template_key TEXT,
    rolled_back_from INTEGER,
    edited_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id, version)
);
CREATE TABLE IF NOT EXISTS intent_rule_previews (
    id UUID PRIMARY KEY,
    operator_key TEXT NOT NULL,
    expected_generation BIGINT NOT NULL,
    command JSONB NOT NULL,
    reference_fingerprints JSONB NOT NULL,
    diff JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_config_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generation BIGINT NOT NULL,
    preview_id UUID NOT NULL,
    operator_key TEXT NOT NULL,
    change JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_template_contracts (
    workflow_key TEXT PRIMARY KEY,
    version BIGINT NOT NULL,
    workflow_digest TEXT NOT NULL,
    input_kind TEXT NOT NULL,
    comment_mode TEXT NOT NULL,
    approve_requires_confirmation BOOLEAN NOT NULL,
    withdraw_requires_confirmation BOOLEAN NOT NULL,
    allow_merge BOOLEAN NOT NULL,
    allow_reviewer_changes BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_hits (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    evaluation_key TEXT NOT NULL,
    intent_id BIGINT,
    discovery_id BIGINT,
    generation BIGINT NOT NULL,
    rule_id UUID,
    rule_version INTEGER,
    selection TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT,
    verified_at TIMESTAMPTZ,
    mr_identity JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS intent_rule_condition_results (
    hit_id BIGINT NOT NULL,
    rule_position INTEGER NOT NULL,
    condition_position INTEGER NOT NULL,
    rule_id UUID NOT NULL,
    rule_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    result TEXT NOT NULL,
    reason TEXT NOT NULL,
    verified_at TIMESTAMPTZ,
    PRIMARY KEY (hit_id, rule_position, condition_position)
);
CREATE TABLE IF NOT EXISTS intent_rule_hit_sources (
    hit_id BIGINT NOT NULL,
    event_seq BIGINT NOT NULL,
    PRIMARY KEY (hit_id, event_seq)
);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS auto_task_daily_cap INTEGER,
    ADD COLUMN IF NOT EXISTS auto_task_config_version BIGINT NOT NULL DEFAULT 0;
"""
# Named constraints permit object-based reentry even if a marker survived an incomplete restore.
for _table, _constraints in _CONSTRAINTS.items():
    for _constraint, _definition in _constraints.items():
        fresh_ddl += f"""
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = '{_table}'::regclass
                   AND conname = '{_constraint}') THEN
        ALTER TABLE {_table} ADD CONSTRAINT {_constraint} {_definition};
    END IF;
END $$;
"""
fresh_ddl += """
CREATE INDEX IF NOT EXISTS idx_intent_rule_hits_rule ON intent_rule_hits(rule_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_intent_rule_hits_created ON intent_rule_hits(created_at, id);
CREATE INDEX IF NOT EXISTS idx_intent_rule_hit_sources_event ON intent_rule_hit_sources(event_seq);
CREATE OR REPLACE FUNCTION intent_rule_versions_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'intent rule versions are append-only';
END $$;
DROP TRIGGER IF EXISTS intent_rule_versions_immutable ON intent_rule_versions;
CREATE TRIGGER intent_rule_versions_immutable BEFORE UPDATE ON intent_rule_versions
    FOR EACH ROW EXECUTE FUNCTION intent_rule_versions_immutable();
CREATE OR REPLACE FUNCTION intent_rule_hit_source_deleted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM intent_rule_hits WHERE id = OLD.hit_id;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS intent_rule_hit_sources_drop ON intent_rule_hit_sources;
CREATE TRIGGER intent_rule_hit_sources_drop AFTER DELETE ON intent_rule_hit_sources
    FOR EACH ROW EXECUTE FUNCTION intent_rule_hit_source_deleted();
"""


async def pending(conn):
    for table, columns in _TABLES.items():
        found = await conn.fetch("SELECT attname FROM pg_attribute WHERE attrelid = to_regclass($1) "
                                 "AND attnum > 0 AND NOT attisdropped", table)
        if not set(columns.split()).issubset({row["attname"] for row in found}):
            return True
    for table, constraints in _CONSTRAINTS.items():
        found = await conn.fetch("SELECT conname FROM pg_constraint WHERE conrelid = to_regclass($1)", table)
        if not set(constraints).issubset({row["conname"] for row in found}):
            return True
    if not await conn.fetchval("SELECT condeferrable AND condeferred FROM pg_constraint "
                               "WHERE conrelid=to_regclass('intent_rules') AND conname='intent_rules_position_key'"):
        return True
    if not await conn.fetchval("SELECT count(*) = 3 FROM pg_index WHERE indisvalid AND indexrelid IN "
        "(to_regclass('idx_intent_rule_hits_rule'),to_regclass('idx_intent_rule_hits_created'),"
        "to_regclass('idx_intent_rule_hit_sources_event'))"):
        return True
    return not await conn.fetchval("SELECT count(*) = 2 FROM pg_trigger WHERE NOT tgisinternal AND "
        "((tgrelid=to_regclass('intent_rule_hit_sources') AND tgname='intent_rule_hit_sources_drop') OR "
        "(tgrelid=to_regclass('intent_rule_versions') AND tgname='intent_rule_versions_immutable'))")


async def apply(conn, *, sql, state):
    if await pending(conn):
        await conn.execute(fresh_ddl)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING", name)
