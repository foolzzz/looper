"""Frozen lesson snapshots, delivery evidence and durable wrong-task identity."""
from __future__ import annotations

from . import OnlineStep
from .memory_lessons import _column, _constraint, _normalized

name = 'memory_lesson_injection'
order = pending_order = online_order = 1940

_TABLES = {
    'task_lesson_snapshot_sets': (
        'task_id TEXT NOT NULL, initialized_at TIMESTAMPTZ NOT NULL, max_lessons INTEGER NOT NULL, '
        'max_chars INTEGER NOT NULL, in_effect_total INTEGER NOT NULL, header TEXT NOT NULL, '
        'footer_template TEXT NOT NULL, format_version INTEGER NOT NULL, omitted INTEGER NOT NULL',
        {'task_lesson_snapshot_sets_pkey': 'PRIMARY KEY (task_id)',
         'task_lesson_snapshot_sets_task_id_fkey': 'FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE',
         'task_lesson_snapshot_sets_bounds_check': 'CHECK (((max_lessons > 0) AND (max_lessons <= 50) AND (max_chars > 0) AND (in_effect_total >= 0) AND (omitted >= 0) AND (format_version > 0)))'}),
    'memory_lesson_wrong_tasks': (
        'candidate_id BIGINT NOT NULL, task_id TEXT NOT NULL, first_injection_id BIGINT, created_at TIMESTAMPTZ NOT NULL',
        {'memory_lesson_wrong_tasks_pkey': 'PRIMARY KEY (candidate_id, task_id)',
         'memory_lesson_wrong_tasks_candidate_id_fkey': 'FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id) ON DELETE CASCADE',
         'memory_lesson_wrong_tasks_task_id_fkey': 'FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE',
         'memory_lesson_wrong_tasks_first_injection_id_fkey': 'FOREIGN KEY (first_injection_id) REFERENCES memory_injections(id) ON DELETE SET NULL'}),
    'task_lesson_planner_state': (
        "task_id TEXT NOT NULL, session_key TEXT, delivered_ids BIGINT[] NOT NULL DEFAULT '{}', "
        "notices_acked BIGINT[] NOT NULL DEFAULT '{}', disabled_acked BOOLEAN NOT NULL DEFAULT false",
        {'task_lesson_planner_state_pkey': 'PRIMARY KEY (task_id)',
         'task_lesson_planner_state_task_id_fkey': 'FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE'}),
}
_COLUMNS = {
    'task_lesson_snapshots': ('line_text TEXT', 'activated_at TIMESTAMPTZ', 'fingerprint TEXT'),
    'memory_injections': ('delivery_key TEXT NOT NULL', 'session_key TEXT', 'delivered_at TIMESTAMPTZ',
                          "state TEXT NOT NULL DEFAULT 'prepared'", "block_text TEXT NOT NULL DEFAULT ''",
                          "candidate_ids BIGINT[] NOT NULL DEFAULT '{}'", "snapshot_key TEXT NOT NULL DEFAULT ''"),
}
_CHECKS = {
    'memory_injections': {
        'memory_injections_delivery_state_check': "CHECK ((state = ANY (ARRAY['prepared'::text, 'delivered'::text, 'abandoned'::text, 'uncertain'::text])))",
        'memory_injections_bounds_check': 'CHECK (((block_chars >= 0) AND (omitted >= 0)))'},
    'memory_injection_items': {
        'memory_injection_items_position_check': 'CHECK ((("position" >= 1) AND ("position" <= 50)))'},
    'memory_lesson_stats': {
        'memory_lesson_stats_nonnegative_check': 'CHECK (((injected >= 0) AND (cited >= 0) AND (wrong_tasks >= 0) AND (reinforced >= 0)))'},
}
_UNIQUE = (('idx_memory_injections_delivery', 'ON memory_injections (task_id, agent_kind, delivery_key)'),
           ('idx_memory_injections_planner_session', "ON memory_injections (task_id, session_key) WHERE ((agent_kind = 'planner'::text) AND (session_key IS NOT NULL) AND (state <> 'abandoned'::text))"))
_INDEXES = (('idx_task_lesson_planner_session', 'ON task_lesson_planner_state (session_key)'),)


def _create(table):
    columns, constraints = _TABLES[table]
    return f'CREATE TABLE IF NOT EXISTS {table} ({columns}, ' + ', '.join(
        f'CONSTRAINT {key} {value}' for key, value in constraints.items()) + ');'


fresh_ddl = ('\n'.join(_create(t) for t in _TABLES) + '\n' + '\n'.join(
    f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column};' for table, columns in _COLUMNS.items() for column in columns) + '\n' +
    '\n'.join(f'ALTER TABLE {table} ADD CONSTRAINT {key} {check};' for table, checks in _CHECKS.items() for key, check in checks.items()) + '\n' +
    '\n'.join(f'CREATE UNIQUE INDEX IF NOT EXISTS {key} {value};' for key, value in _UNIQUE) + '\n' +
    '\n'.join(f'CREATE INDEX IF NOT EXISTS {key} {value};' for key, value in _INDEXES))
online = (OnlineStep(name, 'memory_lesson_injection_online',
                    validate=tuple((table,key) for table,checks in _CHECKS.items() for key in checks),
                    indexes=_INDEXES, unique_indexes=_UNIQUE),)


async def _statements(conn):
    statements = []
    for table,(columns,constraints) in _TABLES.items():
        if not await conn.fetchval('SELECT to_regclass($1) IS NOT NULL',table):
            statements.append(_create(table))
            continue
        for column in columns.split(', '):
            if ddl := await _column(conn,table,column):
                statements.append(ddl)
        for key,value in constraints.items():
            if ddl := await _constraint(conn,table,key,value):
                statements.append(ddl)
    for table,columns in _COLUMNS.items():
        for column in columns:
            exists = await conn.fetchval('SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass($1) '
                'AND attname=$2 AND attnum>0 AND NOT attisdropped)',table,column.split()[0])
            if not exists and table == 'task_lesson_snapshots' and column.startswith('line_text'):
                if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM task_lesson_snapshots)'):
                    raise RuntimeError('legacy lesson snapshots contain no frozen text; operator inspection is required')
            if not exists and column == 'delivery_key TEXT NOT NULL':
                statements.extend(['ALTER TABLE memory_injections ADD COLUMN delivery_key TEXT;',
                    "UPDATE memory_injections SET delivery_key='legacy:' || id::text;",
                    'ALTER TABLE memory_injections ALTER COLUMN delivery_key SET NOT NULL;'])
            elif ddl := await _column(conn,table,column):
                statements.append(ddl)
    for table,checks in _CHECKS.items():
        for key,value in checks.items():
            if ddl := await _constraint(conn,table,key,value):
                statements.append(ddl[:-1] + ' NOT VALID;')
    return statements


async def _indexes_pending(conn):
    missing = False
    for key,definition in _INDEXES + _UNIQUE:
        row = await conn.fetchrow('SELECT indisvalid,indisunique,pg_get_indexdef(indexrelid) AS definition '
            'FROM pg_index WHERE indexrelid=to_regclass($1)',key)
        if row is None or not row['indisvalid']:
            missing = True
        else:
            table_tail = definition.removeprefix('ON ')
            actual = row['definition'].split(' USING ',1)[1]
            expected = 'btree ' + table_tail.split(' ',1)[1]
            if row['indisunique'] != ((key,definition) in _UNIQUE) or _normalized(actual) != _normalized(expected):
                raise RuntimeError('incompatible memory lesson injection index: ' + key)
    return missing


async def _validation_pending(conn):
    return await conn.fetchval('SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname=ANY($1) '
        'AND connamespace=current_schema()::regnamespace AND NOT convalidated)',
        [key for checks in _CHECKS.values() for key in checks])


async def pending(conn):
    return bool(await _statements(conn) or await _indexes_pending(conn) or await _validation_pending(conn))


async def apply(conn, *, sql, state):
    statements = await _statements(conn)
    if not statements and not await _indexes_pending(conn) and not await _validation_pending(conn):
        return
    for ddl in statements:
        await conn.execute(ddl)
    await conn.execute('INSERT INTO schema_migrations(name) VALUES ($1) ON CONFLICT DO NOTHING',name)
    await conn.execute('DELETE FROM schema_migrations WHERE name=$1',online[0].marker)
