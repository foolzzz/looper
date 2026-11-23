"""Decision snapshots on real PostgreSQL: the migration, writes and their dedupe, retention, and the replay command."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg
import pytest

from looper import cli
from looper.agents import housekeeping
from looper.agents.tiers.decisions import Dispatch
from looper.agents.tiers.planner import PlanStep, TaskPlan
from looper.db import connection, decision_snapshots
from tests.bridge_schema_support import structure, witness
from tests.db_support import DSN, drop_tables, fresh_connection, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
TASK = "INSERT INTO projects (id, name) VALUES ('p', 'Example'); INSERT INTO tasks (id, requirement, project_id) " \
       "VALUES ('t1', 'Synthetic requirement', 'p')"


def test_the_upgrade_adds_the_table_once_and_matches_fresh():
    async def test():
        async with fresh_connection() as old, fresh_connection() as fresh:
            await connection.ensure_schema(old)
            restore = await drop_tables(old, "decision_snapshots")
            await old.execute("DELETE FROM schema_migrations WHERE name = 'decision_snapshots'")
            await old.execute(TASK)
            before, shape = await witness(old), await structure(old)
            assert await connection.schema_change_pending(old)
            assert await witness(old) == before and await structure(old) == shape
            await connection.ensure_schema(old)
            await restore()
            await connection.ensure_schema(fresh)
            assert await structure(old) == await structure(fresh)
            after, shape = await witness(old), await structure(old)
            assert not await connection.schema_change_pending(old)
            await connection.ensure_schema(old)
            assert await witness(old) == after and await structure(old) == shape
            assert await old.fetchval("SELECT count(*) FROM schema_migrations WHERE name = 'decision_snapshots'") == 1
    asyncio.run(asyncio.wait_for(test(), 30))


def _dispatch(status: str = "pending") -> tuple[dict, dict]:
    plan = TaskPlan("p", "g", "r", "/w", steps=[PlanStep("id-s1", 0, "do s1", key="s1", status=status)])
    ready = plan.ready_steps()
    record = Dispatch(plan, isolate_steps=False)
    return record.inputs, record.decision(ready, ready)


def test_writes_dedupe_a_repeated_dispatch_and_go_with_their_task(monkeypatch):
    async def test(pool):
        await pool.execute(TASK)
        inputs, decision = _dispatch()
        add = decision_snapshots.add
        first = await add("t1", "dispatch", inputs=inputs, decision=decision)
        assert await add("t1", "dispatch", inputs=inputs, decision=decision) is None  # nothing changed
        other_inputs, other = _dispatch("verified")
        assert await add("t1", "dispatch", inputs=other_inputs, decision=other) is not None
        assert await add("t1", "dispatch", inputs=inputs, decision=decision) is not None  # differs from the last
        steps = [await add("t1", "step_decision", step_id="id-s1", record_id=7, inputs={"version": 1},
                           decision={"decision": "accept"}) for _ in range(2)]
        assert None not in steps
        rows = await decision_snapshots.for_task("t1")
        assert [row["id"] for row in rows] == sorted(row["id"] for row in rows) and rows[0]["id"] == first
        assert [row["kind"] for row in rows] == ["dispatch"] * 3 + ["step_decision"] * 2
        assert rows[0]["inputs"] == inputs and rows[0]["decision"] == decision and rows[3]["record_id"] == 7
        with pytest.raises(asyncpg.CheckViolationError):
            await add("t1", "step_decision", inputs={}, decision={})  # a step decision names its step
        await pool.execute("DELETE FROM tasks WHERE id = 't1'")
        assert await pool.fetchval("SELECT count(*) FROM decision_snapshots") == 0

    run_db(test, monkeypatch, timeout=30)


def test_the_switch_and_a_turns_record(monkeypatch):
    async def test(pool):
        assert not await decision_snapshots.enabled()
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", decision_snapshots.ENABLED_KEY)
        assert await decision_snapshots.enabled()
        record = await pool.fetchval(
            "INSERT INTO llm_records (agent_id, input_text, metadata, input_tokens, context_tokens) "
            "VALUES ('planner-t1', 'synthetic turn', $1::jsonb, 12, 34) RETURNING id",
            json.dumps({"system_prompt_sha256": "f" * 64}))
        assert await decision_snapshots.turn(record) == {
            "id": record, "metadata": {"system_prompt_sha256": "f" * 64}, "input_tokens": 12, "context_tokens": 34,
            "cache_read_tokens": None, "input_chars": len("synthetic turn")}
        assert await decision_snapshots.turn(record + 1) is None

    run_db(test, monkeypatch, timeout=30)


@pytest.mark.parametrize("on", [True, False])
def test_housekeeping_deletes_snapshots_after_thirty_days_only_while_on(monkeypatch, tmp_path, on):
    async def test(pool):
        await pool.execute(TASK)
        for age in (31, 31, 29):
            inputs, decision = _dispatch()
            row = await decision_snapshots.add("t1", "step_decision", step_id="id-s1", inputs=inputs,
                                               decision=decision)
            await pool.execute("UPDATE decision_snapshots SET created_at = $1 WHERE id = $2",
                               NOW - timedelta(days=age), row)
        if on:
            await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')",
                               decision_snapshots.ENABLED_KEY)
        from looper.agents import chat_settings

        monkeypatch.setattr(chat_settings, "_metrics_enabled", True)
        await pool.execute("INSERT INTO proactive_deliveries (source, channel_id, chat, chat_kind, outcome, sent_at) "
                           "VALUES ('gate', 'synthetic-channel', 'synthetic-chat', 'direct', 'sent', $1)",
                           NOW - timedelta(days=91))
        deleted = []
        prune = decision_snapshots.prune

        async def counted(conn, before):
            deleted.append(before)
            return await prune(conn, before)

        monkeypatch.setattr(decision_snapshots, "prune", counted)
        report = await housekeeping.run_once(tmp_path, NOW, current_runner=None, commands=None)
        assert not report.failed_steps
        assert report.proactive_deliveries == 1
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        # Off, housekeeping writes nothing to the table: the rows of an earlier window stay with their task.
        assert report.decision_snapshots == (2 if on else 0) and bool(deleted) is on
        assert await pool.fetchval("SELECT count(*) FROM decision_snapshots") == (1 if on else 3)

    run_db(test, monkeypatch, timeout=60)


def test_a_locked_table_or_settings_row_never_holds_the_decision(monkeypatch, caplog):
    from looper.agents.tiers import decisions

    monkeypatch.setattr(decisions, "TIMEOUT_SECONDS", 0.3)

    async def test(pool):
        await pool.execute(TASK)
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ($1, 'true')", decision_snapshots.ENABLED_KEY)
        plan = TaskPlan("p", "g", "r", "/w", steps=[PlanStep("id-s1", 0, "do s1", key="s1")])
        async with pool.acquire() as locker, locker.transaction():
            await locker.execute("LOCK TABLE decision_snapshots IN ACCESS EXCLUSIVE MODE")
            started = asyncio.get_running_loop().time()
            await Dispatch(plan, isolate_steps=False).save("t1", plan.steps, plan.steps)
            assert asyncio.get_running_loop().time() - started < 2
            await locker.execute("LOCK TABLE system_settings IN ACCESS EXCLUSIVE MODE")
            started = asyncio.get_running_loop().time()
            assert await decisions.enabled() is False
            assert asyncio.get_running_loop().time() - started < 2
        assert await pool.fetchval("SELECT count(*) FROM decision_snapshots") == 0
        assert "timed out" in caplog.text
        assert await decisions.enabled() is True  # the lock gone, the switch reads again

    run_db(test, monkeypatch, timeout=30, max_size=4)


def test_replay_command_compares_each_snapshot(monkeypatch, tmp_path, capsys):
    async def test(pool):
        await pool.execute(TASK)
        schema = await pool.fetchval("SELECT current_schema()")
        parts = urlsplit(DSN)
        url = urlunsplit(parts._replace(query=urlencode([*parse_qsl(parts.query), ("search_path", schema)])))
        config = tmp_path / "server.toml"
        config.write_text(f'[database]\nurl = "{url}"\n')
        config.chmod(0o600)

        async def replay(*argv: str) -> int:
            return await asyncio.to_thread(cli.main, ["decisions", "replay", *argv, "--config", str(config),
                                                      "--runtime-root", str(tmp_path / "runtime")])

        assert await replay("t1") == 1
        assert "has no decision snapshots" in capsys.readouterr().err
        inputs, decision = _dispatch()
        # The command opens and closes a pool of its own: write through this one.
        row = await decision_snapshots.add("t1", "dispatch", inputs=inputs, decision=decision, db=pool)
        assert await replay("t1") == 0
        out = capsys.readouterr().out
        assert f"#{row} " in out and "dispatch: same; ready s1 (work); start s1" in out
        assert "1 snapshot(s): 1 same, 0 differ" in out
        await pool.execute("UPDATE decision_snapshots SET decision = jsonb_set(decision, '{start}', '[]') "
                           "WHERE id = $1", row)
        assert await replay("t1") == 3
        out = capsys.readouterr().out
        assert "DIFFERS" in out and "recorded: ready s1 (work); start none" in out
        assert "replayed: ready s1 (work); start s1" in out
        assert await replay("t1", "--json") == 3
        [answer] = json.loads(capsys.readouterr().out)
        assert answer["same"] is False and answer["replayed"]["start"] == ["id-s1"]

    run_db(test, monkeypatch, timeout=60)
