"""Lesson storage: fresh/upgrade equivalence, evidence lifetime and atomic transitions."""

import asyncio
from datetime import timedelta

import asyncpg
import pytest

from looper.agents.memory_model import Action, Actor, State, Via
from looper.db import connection, memory
from tests import memory_support as sup
from tests.bridge_schema_support import structure
from tests.db_support import DSN, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
TABLES = ("task_lesson_snapshots", "memory_injections", "memory_injection_items", "memory_lesson_stats",
          "memory_retro_runs", "memory_weekly_cards")


async def lesson(db, *, state="held", reason="shadow", project_id="proj-mem", **fields):
    if not await db.fetchval("SELECT EXISTS(SELECT 1 FROM tasks WHERE id='task-mem')"):
        journal = await sup.journal_entry(db)
    else:
        step = "s" + str(await db.fetchval("SELECT count(*) + 1 FROM task_journal WHERE task_id='task-mem'"))
        journal = await db.fetchval("INSERT INTO task_journal(task_id,step_id,kind,summary) "
                                    "VALUES ('task-mem',$1,'step_accepted','Evidence') RETURNING id", step)
    values = dict(destination="looper", kind="lesson", source_kind="task", source_class="agent",
                  statement="Run the generator", fingerprint="a" * 64, project_id=project_id,
                  valid_until=sup.NOW + timedelta(days=90), state=state, reason=reason) | fields
    names = list(values)
    candidate = await db.fetchval(
        f"INSERT INTO memory_candidates ({', '.join(names)}) VALUES "
        f"({', '.join('$' + str(i + 1) for i in range(len(names)))}) RETURNING id", *values.values())
    await db.execute("INSERT INTO memory_candidate_sources (candidate_id,kind,journal_id) VALUES ($1,'journal',$2)",
                     candidate, journal)
    return candidate, journal


def test_fresh_and_upgraded_lesson_schema_are_equivalent():
    from looper.db import migrations
    from looper.db.migrations import memory_lessons as migration

    async def check():
        async with fresh_connection() as upgraded, fresh_connection() as fresh:
            # Later migrations may depend on the lesson tables: start before the foundation.
            old = tuple(m for m in connection._MIGRATIONS if m.order < migration.order)
            await upgraded.execute(migrations.schema_sql(connection.SCHEMA_PATH.read_text(), old))
            for table in TABLES:
                assert await upgraded.fetchval("SELECT to_regclass($1)", table) is None
            await upgraded.execute("INSERT INTO registry_schema_version(id,version) VALUES(TRUE,$1)",
                                   connection.REGISTRY_SCHEMA_VERSION)
            from looper.db.bridge_upgrade import BASELINE
            await upgraded.execute("INSERT INTO schema_migrations(name) VALUES ($1)", BASELINE)
            before = await sup.candidate(upgraded)
            assert await migration.pending(upgraded)
            await connection.ensure_schema(upgraded)
            await connection.ensure_schema(fresh)
            assert await structure(upgraded) == await structure(fresh)
            assert not await connection.schema_change_pending(upgraded)
            assert not await connection.schema_change_pending(fresh)
            assert await upgraded.fetchval("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE name=$1)", migration.name)
            assert {r[0] for r in await fresh.fetch("SELECT name FROM schema_migrations")} == {BASELINE}
            candidate, _ = await lesson(upgraded)
            await connection.ensure_schema(upgraded)
            assert await upgraded.fetchval("SELECT count(*) FROM memory_candidates WHERE id=ANY($1)",
                                           [before, candidate]) == 2
            await upgraded.execute("ALTER TABLE memory_lesson_stats DROP COLUMN reinforced")
            assert await migration.pending(upgraded)
            await connection.ensure_schema(upgraded)
            assert not await migration.pending(upgraded)
            await upgraded.execute("ALTER TABLE memory_lesson_stats ALTER COLUMN reinforced TYPE bigint")
            with pytest.raises(RuntimeError, match="incompatible"):
                await connection.ensure_schema(upgraded)
    asyncio.run(check())


def test_lesson_constraints_and_source_deletion(monkeypatch):
    async def check(pool):
        candidate, journal = await lesson(pool, state="in_effect", reason=None)
        for changes in ({"source_class": "external"}, {"source_class": "system"}, {"project_id": None},
                        {"valid_until": None}):
            key, value = next(iter(changes.items()))
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(f"UPDATE memory_candidates SET {key}=$2 WHERE id=$1", candidate, value)
        await pool.execute("INSERT INTO task_lesson_snapshots VALUES ('task-mem',$1,1)", candidate)
        other, _ = await lesson(pool)
        with pytest.raises(asyncpg.UniqueViolationError):
            await pool.execute("INSERT INTO task_lesson_snapshots VALUES ('task-mem',$1,1)", other)
        injection = await pool.fetchval("INSERT INTO memory_injections(task_id,agent_kind,block_sha256,block_chars) "
                                        "VALUES ('task-mem','worker',$1,100) RETURNING id", "b" * 64)
        await pool.execute("INSERT INTO memory_injection_items VALUES ($1,$2,1,false,false)", injection, candidate)
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE memory_injection_items SET cited=true,wrong=true WHERE injection_id=$1", injection)
        await pool.execute("INSERT INTO memory_lesson_stats(candidate_id) VALUES ($1)", candidate)
        await pool.execute("INSERT INTO memory_retro_runs(task_id,journal_from_id,journal_to_id) "
                           "VALUES ('task-mem',1,2)")
        record = await pool.fetchval("INSERT INTO llm_records(agent_id) VALUES ('worker-synthetic') RETURNING id")
        await pool.execute("UPDATE memory_injections SET record_id=$1", record)
        await pool.execute("UPDATE memory_retro_runs SET record_id=$1", record)
        await pool.execute("DELETE FROM llm_records WHERE id=$1", record)
        assert await pool.fetchval("SELECT record_id IS NULL FROM memory_injections")
        assert await pool.fetchval("SELECT record_id IS NULL FROM memory_retro_runs")
        for changes in ("state='skipped'", "skip_reason='budget'", "journal_from_id=3", "counts='[]'"):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute("UPDATE memory_retro_runs SET " + changes)
        await pool.execute("INSERT INTO memory_weekly_cards(week_start) VALUES ('2026-11-23')")
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("UPDATE memory_weekly_cards SET counts='[]'")
        vault = await sup.candidate(pool, project_id="proj-mem")
        await pool.execute("DELETE FROM task_journal WHERE id=$1", journal)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id=$1", candidate) == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_lesson_stats") == 0
        assert await pool.fetchval("SELECT count(*) FROM memory_injection_items") == 0
        await pool.execute("DELETE FROM tasks WHERE id='task-mem'")
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id=$1", other) == 0
        for table in ("task_lesson_snapshots", "memory_injections", "memory_retro_runs"):
            assert await pool.fetchval(f"SELECT count(*) FROM {table}") == 0
        project_lesson, _ = await lesson(pool)
        await pool.execute("INSERT INTO projects(id,name) VALUES ('proj-other','Other synthetic project')")
        await pool.execute("UPDATE tasks SET project_id='proj-other' WHERE id='task-mem'")
        await pool.execute("DELETE FROM projects WHERE id='proj-mem'")
        assert await pool.fetchval("SELECT count(*) FROM memory_candidates WHERE id=$1", project_lesson) == 0
        assert await pool.fetchval("SELECT project_id IS NULL FROM memory_candidates WHERE id=$1", vault)
    run_db(check, monkeypatch, timeout=30)


def test_lesson_transition_is_atomic_and_retains_tombstones(monkeypatch):
    from looper.db.memory_lessons import transition_lesson

    async def check(pool):
        candidate, _ = await lesson(pool)
        async with pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="transaction"):
                await transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE, actor=Actor.USER,
                                        via=Via.DASHBOARD, now=sup.NOW, valid_days=90)
        async with memory.transaction(pool) as conn:
            row = await transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE, actor=Actor.USER,
                                          via=Via.DASHBOARD, now=sup.NOW, valid_days=90)
            assert row["state"] == "in_effect" and row["reason"] is None
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1", candidate) == 1
        with pytest.raises(asyncpg.PostgresError):
            async with memory.transaction(pool) as conn:
                await transition_lesson(conn, candidate, State.REVOKED, action=Action.REVOKE, actor=Actor.USER,
                                        via=Via.LARK, now=sup.NOW, valid_days=90)
                await conn.execute("SELECT 1/0")
        assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", candidate) == "in_effect"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1", candidate) == 1
        assert await pool.fetchval("SELECT count(*) FROM memory_tombstones") == 0
        async with memory.transaction(pool) as conn:
            await transition_lesson(conn, candidate, State.REVOKED, action=Action.REVOKE, actor=Actor.USER,
                                    via=Via.LARK, now=sup.NOW, valid_days=90)
        assert await pool.fetchval("SELECT deleted_via FROM memory_tombstones") == "lark"
        held, _ = await lesson(pool)
        async with memory.transaction(pool) as conn:
            await transition_lesson(conn, held, State.REJECTED, action=Action.REJECT, actor=Actor.USER,
                                    via=Via.TELEGRAM, now=sup.NOW, valid_days=90)
        assert await pool.fetchval("SELECT deleted_via FROM memory_tombstones") == "telegram"
        expired, _ = await lesson(pool, fingerprint="c" * 64)
        async with memory.transaction(pool) as conn:
            await transition_lesson(conn, expired, State.EXPIRED, action=Action.EXPIRE, actor=Actor.SYSTEM,
                                    via=Via.SYSTEM, now=sup.NOW, valid_days=90)
        assert not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM memory_tombstones WHERE fingerprint=$1)", "c" * 64)
        vault = await sup.candidate(pool)
        async with memory.transaction(pool) as conn:
            with pytest.raises(memory.MemoryRefused):
                await transition_lesson(conn, vault, State.EXPIRED, action=Action.EXPIRE, actor=Actor.SYSTEM,
                                        via=Via.SYSTEM, now=sup.NOW, valid_days=90)
            with pytest.raises(memory.MemoryRefused):
                await memory.transition(conn, expired, State.PENDING, action=Action.REVIVE, actor=Actor.USER,
                                        via=Via.CLI, now=sup.NOW)
    run_db(check, monkeypatch, timeout=30)


def test_concurrent_lesson_transitions_reject_the_stale_loser(monkeypatch):
    from looper.db.memory_lessons import transition_lesson

    async def check(pool):
        candidate, _ = await lesson(pool)
        async def activate():
            try:
                async with memory.transaction(pool) as conn:
                    return await transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.ACTIVATE,
                        actor=Actor.USER, via=Via.DASHBOARD, now=sup.NOW, valid_days=90, expected_state=State.HELD)
            except memory.MemoryRefused as error:
                return error
        results = await asyncio.gather(activate(), activate())
        assert sum(isinstance(result, memory.MemoryRefused) for result in results) == 1
        assert next(result for result in results if isinstance(result, memory.MemoryRefused)).code == "stale"
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1", candidate) == 1
    run_db(check, monkeypatch, timeout=30)


def test_lesson_schema_refuses_a_changed_predicate_grouping():
    from looper.db.migrations import memory_lessons as migration

    async def check(pool):
        await pool.execute("ALTER TABLE memory_injection_items DROP CONSTRAINT memory_injection_items_check; "
                           "ALTER TABLE memory_injection_items ADD CONSTRAINT memory_injection_items_check "
                           "CHECK (NOT cited AND wrong)")
        async with pool.acquire() as conn:
            with pytest.raises(RuntimeError, match="incompatible.*constraint"):
                await migration.pending(conn)
    run_db(check, timeout=30)


def test_lesson_schema_repairs_missing_objects_even_after_markers():
    from looper.db.migrations import memory_lessons as migration

    async def check(pool):
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1),($2) ON CONFLICT DO NOTHING",
                               migration.name, migration.online[0].marker)
            await conn.execute("DROP TABLE memory_weekly_cards; "
                               "ALTER TABLE memory_injection_items DROP CONSTRAINT memory_injection_items_check; "
                               "DROP INDEX idx_memory_candidates_lesson_expiry; "
                               "ALTER TABLE memory_tombstones DROP CONSTRAINT memory_tombstones_deleted_via_check; "
                               "ALTER TABLE memory_tombstones ADD CONSTRAINT memory_tombstones_deleted_via_check " +
                               migration._TOMBSTONE_CHECK + " NOT VALID")
            assert await migration.pending(conn)
            await connection.ensure_schema(conn)
            assert not await migration.pending(conn)
            assert await conn.fetchval("SELECT convalidated FROM pg_constraint "
                "WHERE conrelid='memory_tombstones'::regclass AND conname='memory_tombstones_deleted_via_check'")
    run_db(check, timeout=30)


def test_renewals_validate_before_recording_an_event(monkeypatch):
    from looper.db.memory_lessons import transition_lesson

    async def check(pool):
        candidate, _ = await lesson(pool, state="in_effect", reason=None)
        async with memory.transaction(pool) as conn:
            with pytest.raises(memory.MemoryRefused, match="timestamp range"):
                await transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.RENEW, actor=Actor.USER,
                    via=Via.CLI, now=sup.NOW, valid_days=10**100)
        assert await pool.fetchval("SELECT count(*) FROM memory_candidate_events WHERE candidate_id=$1", candidate) == 0
        later = sup.NOW + timedelta(days=1)
        async with memory.transaction(pool) as conn:
            row = await transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.RENEW, actor=Actor.USER,
                    via=Via.CLI, now=later, valid_days=30)
        assert row["valid_until"] == later + timedelta(days=30)
        async with memory.transaction(pool) as conn:
            row = await transition_lesson(conn, candidate, State.IN_EFFECT, action=Action.REINFORCE, actor=Actor.SYSTEM,
                    via=Via.SYSTEM, now=later, valid_days=60)
        assert row["valid_until"] == later + timedelta(days=60)
    run_db(check, monkeypatch, timeout=30)
