"""Opt-in collection, independent parity identities and durable alert episodes."""
from __future__ import annotations

import re

from . import OnlineStep, _widen_check, printed_check

name = "message_collection"
order = pending_order = online_order = 1950

_ERRORS = "'auth','identity','protocol','pagination','transient','rate_limit','permission','left','budget','interrupted','connection','policy','target','delivery','unknown'"
_STATES = "'pending','sending','sent','rejected','unknown','dashboard','deferred'"
# All new objects are small additive tables. Keep their declarations here for fresh creation and repair.
_TABLES = {
    "source_collection_settings": (
        {
            "source_id": "TEXT NOT NULL", "collect_cron": "TEXT NOT NULL DEFAULT '*/10 * * * *'",
            "alerts_enabled": "BOOLEAN NOT NULL DEFAULT false", "generation": "BIGINT NOT NULL DEFAULT 0",
            "mode_changed_at": "TIMESTAMPTZ", "collect_enabled_at": "TIMESTAMPTZ", "last_collect_ok_at": "TIMESTAMPTZ",
            "last_fire_at": "TIMESTAMPTZ",
        }, {
            "pkey": "PRIMARY KEY (source_id)",
            "source_id_fkey": "FOREIGN KEY (source_id) REFERENCES event_sources(id) ON DELETE CASCADE",
            "generation_check": "CHECK (generation >= 0)",
        }),
    "source_parity_sessions": (
        {
            "id": "BIGSERIAL NOT NULL", "source_id": "TEXT NOT NULL", "generation": "BIGINT NOT NULL",
            "window_from": "TIMESTAMPTZ NOT NULL", "window_to": "TIMESTAMPTZ NOT NULL", "baseline_seq": "BIGINT NOT NULL",
            "created_at": "TIMESTAMPTZ NOT NULL DEFAULT now()", "finished_at": "TIMESTAMPTZ",
            "state": "TEXT NOT NULL DEFAULT 'pending'", "reason": "TEXT", "policy_digest": "TEXT NOT NULL",
            "reference_digest": "TEXT NOT NULL", "reference_total": "BIGINT NOT NULL DEFAULT 0",
            "observed_total": "BIGINT NOT NULL DEFAULT 0", "stored_only": "BIGINT NOT NULL DEFAULT 0",
        }, {
            "pkey": "PRIMARY KEY (id)",
            "source_id_fkey": "FOREIGN KEY (source_id) REFERENCES event_sources(id) ON DELETE CASCADE",
            "generation_check": "CHECK (generation >= 0)",
            "window_check": "CHECK (window_to > window_from AND window_to <= window_from + '24:00:00'::interval)",
            "state_check": "CHECK (state IN ('pending','matched','drift','incomplete'))",
            "reason_check": "CHECK (reason IN ('reference_changed','reference_invalid','coverage_missing','collection_failed','policy_changed','evidence_deleted','identity_conflict'))",
            "counts_check": "CHECK (reference_total >= 0 AND observed_total >= 0 AND stored_only >= 0 AND baseline_seq >= 0)",
        }),
    "source_parity_members": (
        {
            "session_id": "BIGINT NOT NULL", "side": "TEXT NOT NULL", "external_id": "TEXT NOT NULL",
            "chat_id": "TEXT NOT NULL", "sent_at": "TIMESTAMPTZ NOT NULL", "thread_id": "TEXT", "root_id": "TEXT", "parent_id": "TEXT",
        }, {
            "pkey": "PRIMARY KEY (session_id, side, external_id)",
            "session_id_fkey": "FOREIGN KEY (session_id) REFERENCES source_parity_sessions(id) ON DELETE CASCADE",
            "side_check": "CHECK (side IN ('bridge','collect'))",
        }),
    "source_parity_coverage": (
        {
            "session_id": "BIGINT NOT NULL", "chat_id": "TEXT NOT NULL", "container_kind": "TEXT NOT NULL",
            "container_id": "TEXT NOT NULL", "window_from": "TIMESTAMPTZ NOT NULL", "window_to": "TIMESTAMPTZ NOT NULL",
            "state": "TEXT NOT NULL DEFAULT 'pending'", "error": "TEXT",
        }, {
            "pkey": "PRIMARY KEY (session_id, container_kind, container_id)",
            "session_id_fkey": "FOREIGN KEY (session_id) REFERENCES source_parity_sessions(id) ON DELETE CASCADE",
            "container_kind_check": "CHECK (container_kind IN ('chat','thread'))",
            "container_id_check": "CHECK (container_id <> '')",
            "window_check": "CHECK (window_to > window_from)",
            "state_check": "CHECK (state IN ('pending','complete','failed'))",
            "error_check": f"CHECK (error IN ({_ERRORS}))",
        }),
    "source_stream_alerts": (
        {
            "id": "BIGSERIAL NOT NULL", "source_id": "TEXT NOT NULL", "kind": "TEXT NOT NULL",
            "opened_at": "TIMESTAMPTZ NOT NULL", "resolved_at": "TIMESTAMPTZ", "parity_session_id": "BIGINT",
            "metadata": "JSONB NOT NULL DEFAULT '{}'", "target_channel": "TEXT", "target_chat": "TEXT",
            "open_state": "TEXT NOT NULL DEFAULT 'pending'", "recovery_state": "TEXT NOT NULL DEFAULT 'pending'",
            "open_claim": "UUID", "recovery_claim": "UUID", "open_next_at": "TIMESTAMPTZ", "recovery_next_at": "TIMESTAMPTZ",
            "open_attempts": "INTEGER NOT NULL DEFAULT 0", "recovery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "open_error": "TEXT", "recovery_error": "TEXT",
        }, {
            "pkey": "PRIMARY KEY (id)",
            "source_id_fkey": "FOREIGN KEY (source_id) REFERENCES event_sources(id) ON DELETE CASCADE",
            "parity_session_id_fkey": "FOREIGN KEY (parity_session_id) REFERENCES source_parity_sessions(id) ON DELETE SET NULL",
            "kind_check": "CHECK (kind IN ('parity_drift','collector_failure','stale','auth','auth_expiring','partial'))",
            "open_state_check": f"CHECK (open_state IN ({_STATES}))",
            "recovery_state_check": f"CHECK (recovery_state IN ({_STATES}))",
            "open_attempts_check": "CHECK (open_attempts >= 0 AND open_attempts <= 5)",
            "recovery_attempts_check": "CHECK (recovery_attempts >= 0 AND recovery_attempts <= 5)",
            "open_error_check": f"CHECK (open_error IN ({_ERRORS}))",
            "recovery_error_check": f"CHECK (recovery_error IN ({_ERRORS}))",
        }),
}
_INDEXES = {
    "idx_source_collection_alerts": "ON source_collection_settings (source_id) WHERE alerts_enabled",
    "idx_source_parity_sessions_source": "ON source_parity_sessions (source_id, id DESC)",
    "idx_source_parity_members_bucket": "ON source_parity_members (session_id, side, chat_id, sent_at, external_id)",
    "idx_source_stream_alerts_active": "UNIQUE ON source_stream_alerts (source_id, kind) WHERE resolved_at IS NULL",
    "idx_source_stream_alerts_attention": "ON source_stream_alerts (opened_at, id) WHERE resolved_at IS NULL",
    "idx_source_stream_alerts_resolved": "ON source_stream_alerts (resolved_at, id) WHERE resolved_at IS NOT NULL",
}
_TABLE = "proactive_deliveries"
_CHECK = "proactive_deliveries_source_check"
_ADD = ("message_stream_alert",)
_WIDEN = _widen_check.ddl(_TABLE, "source", _CHECK, _ADD)


def _create_table(table, columns, constraints):
    entries = [f"{column} {ddl}" for column, ddl in columns.items()]
    entries += [f"CONSTRAINT {table}_{suffix} {ddl}" for suffix, ddl in constraints.items()]
    return f"CREATE TABLE IF NOT EXISTS {table} (" + ",\n".join(entries) + ");\n"


def _index_ddl(index, definition):
    unique = definition.startswith("UNIQUE ")
    suffix = definition.removeprefix("UNIQUE ")
    return f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {index} {suffix};\n"


fresh_ddl = "".join(_create_table(t, *spec) for t, spec in _TABLES.items()) + "".join(
    _index_ddl(i, definition) for i, definition in _INDEXES.items()) + _WIDEN + f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CHECK};\n"
online = (OnlineStep(name, "message_collection_online", validate=((_TABLE, _CHECK),)),)


def _normal(definition):
    # PostgreSQL prints IN as = ANY(ARRAY[]); compare the whole expression, including operators and bounds.
    result = printed_check(definition).replace("=any(array[", "in(").replace("])", ")")
    result = re.sub(r"::(?:text|bigint|integer|interval)", "", result)
    return result.replace("(", "").replace(")", "").replace('"', "")


async def _missing(conn):
    repairs = []
    types = {"TEXT": "text", "BIGINT": "bigint", "BIGSERIAL": "bigint", "BOOLEAN": "boolean",
             "TIMESTAMPTZ": "timestamp with time zone", "INTEGER": "integer", "JSONB": "jsonb", "UUID": "uuid"}
    for table, (columns, constraints) in _TABLES.items():
        if not await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table):
            repairs.append(_create_table(table, columns, constraints))
            continue
        actual = {r["attname"]: r for r in await conn.fetch(
            "SELECT a.attname, format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull, "
            "pg_get_expr(d.adbin,d.adrelid) AS default_expr FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE a.attrelid=to_regclass($1) AND a.attnum>0 AND NOT a.attisdropped", table)}
        for column, definition in columns.items():
            if column not in actual:
                repairs.append(f"ALTER TABLE {table} ADD COLUMN {column} {definition};")
                continue
            expected_type = types[definition.split()[0]]
            if actual[column]["type"] != expected_type or actual[column]["attnotnull"] != ("NOT NULL" in definition):
                raise RuntimeError(f"incompatible {table}.{column} definition")
            default = actual[column]["default_expr"]
            if definition.startswith("BIGSERIAL"):
                if not default or not default.startswith("nextval(") or not await conn.fetchval(
                        "SELECT pg_get_serial_sequence($1,$2)", table, column):
                    raise RuntimeError(f"incompatible {table}.{column} sequence default")
            else:
                expected_default = definition.split(" DEFAULT ", 1)[1] if " DEFAULT " in definition else None
                if expected_default is not None and default is None:
                    repairs.append(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {expected_default};")
                elif ((default is None) != (expected_default is None) or default is not None and
                      _normal(default.replace("::jsonb", "")) != _normal(expected_default)):
                    raise RuntimeError(f"incompatible {table}.{column} default")
        found = {r["conname"]: r for r in await conn.fetch(
            "SELECT conname,pg_get_constraintdef(oid) AS definition,convalidated FROM pg_constraint WHERE conrelid=to_regclass($1)", table)}
        for suffix, definition in constraints.items():
            key = f"{table}_{suffix}"
            if key not in found:
                repairs.append(f"ALTER TABLE {table} ADD CONSTRAINT {key} {definition};")
            elif _normal(found[key]["definition"]) != _normal(definition):
                raise RuntimeError(f"incompatible {key} definition")
            elif not found[key]["convalidated"]:
                repairs.append(f"ALTER TABLE {table} VALIDATE CONSTRAINT {key};")
    for index, definition in _INDEXES.items():
        found = await conn.fetchval("SELECT pg_get_indexdef(to_regclass($1))", index)
        if found is None:
            repairs.append(_index_ddl(index, definition))
        else:
            expected = _index_ddl(index, definition).replace(" IF NOT EXISTS", "").strip().removesuffix(";")
            # Schema qualification and the explicit default btree method are immaterial.
            found = re.sub(r" ON [^ .]+\.", " ON ", found).replace(" USING btree", "")
            if _normal(found) != _normal(expected):
                raise RuntimeError(f"incompatible {index} definition")
    return repairs


async def pending(conn):
    return bool(await _missing(conn)) or await _widen_check.pending(conn, _TABLE, "source", _ADD)


async def apply(conn, *, sql, state) -> bool:
    did_work = False
    repairs = await _missing(conn)
    widen = await _widen_check.pending(conn, _TABLE, "source", _ADD)
    from .. import connection
    unvalidated = await connection._unvalidated(conn, name)
    if repairs or widen or unvalidated:
        did_work = True
        for ddl in repairs:
            await conn.execute(ddl)
        if widen:
            await conn.execute(_WIDEN)
        await conn.execute("INSERT INTO schema_migrations(name) VALUES($1) ON CONFLICT DO NOTHING", name)
        await conn.execute("DELETE FROM schema_migrations WHERE name=$1", online[0].marker)
    return did_work
