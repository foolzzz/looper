"""The drafting run kind and role upgrade without rewriting old run rows."""

import asyncio

import pytest

from looper.agents import agent_settings
from looper.agents.serve import seed_role_configs
from looper.db import connection, llm_records, role_configs
from looper.db.migrations import memory_draft_role
from tests.db_support import DSN, fresh_schema

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_drafting_checks_upgrade_online_and_seed_the_role(monkeypatch):
    async def check():
        async with fresh_schema() as pool:
            await connection.ensure_schema(pool)
            monkeypatch.setattr(connection, "_pool", pool)
            await pool.execute("DELETE FROM schema_migrations WHERE name IN ('memory_draft_role', 'memory_draft_role_online')")
            for table, constraint, column, values in (
                ("llm_records", "llm_records_run_kind_check", "run_kind", llm_records.RUN_KINDS),
                ("agent_setting_layers", "agent_setting_layers_role_check", "role",
                 tuple(role.value for role in agent_settings.AgentType)),
            ):
                choices = ", ".join(f"'{value}'" for value in values if value != "memory_draft")
                await pool.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}, "
                                   f"ADD CONSTRAINT {constraint} CHECK ({column} IN ({choices}))")
            statements = await memory_draft_role.statements(pool)
            assert len(statements) == 2 and all("NOT VALID" in sql for sql in statements)
            # The real startup applies the widening and validates after its schema transaction commits.
            await connection.ensure_schema(pool)
            checks = await pool.fetch("SELECT convalidated, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
                                      "WHERE conname IN ('llm_records_run_kind_check', 'agent_setting_layers_role_check') "
                                      "AND connamespace = current_schema()::regnamespace")
            assert len(checks) == 2
            assert all(row["convalidated"] and "memory_draft" in row["definition"] for row in checks)
            record = await llm_records.start_record(agent_id="memory_draft-synthetic", run_kind="memory_draft")
            assert await pool.fetchval("SELECT run_kind FROM llm_records WHERE id = $1", record) == "memory_draft"
            await seed_role_configs(pool)
            role = await role_configs.get_config("memory_draft")
            assert role["timeout_seconds"] == 120 and role["max_turns"] == 1
            await pool.execute("INSERT INTO agent_setting_layers (scope, role) VALUES ('role', 'memory_draft')")
            resolved = await agent_settings.load_agent_settings(agent_settings.AgentType.MEMORY_DRAFT)
            assert resolved.profile.name == "claude-code/sonnet-5-5"
            assert not await memory_draft_role.statements(pool)

    asyncio.run(check())


def test_drafting_queue_upgrades_existing_candidate_tables(monkeypatch):
    from tests.db_support import run_db
    from tests import memory_support as sup

    async def check(pool):
        cid = await sup.candidate(pool)
        await pool.execute("DROP TABLE memory_draft_waits, memory_draft_lease; "
                           "DELETE FROM schema_migrations WHERE name='memory_draft_queue'")
        async with pool.acquire() as conn:
            assert await connection.schema_change_pending(conn)
        await connection.ensure_schema(pool)
        assert await pool.fetchval("SELECT state FROM memory_candidates WHERE id=$1", cid) == "drafting"
        assert await pool.fetchval("SELECT to_regclass('memory_draft_waits') IS NOT NULL "
                                   "AND to_regclass('memory_draft_lease') IS NOT NULL")
        await connection.ensure_schema(pool)
        async with pool.acquire() as conn:
            assert not await connection.schema_change_pending(conn)

    run_db(check, monkeypatch)


def test_a_private_draft_records_only_a_digest_and_typed_events(monkeypatch, tmp_path):
    import json

    from looper.agents import model_call
    from looper.agents.memory_draft_role import system_prompt
    from looper.agents.spawn import ChildRun, SpawnKind, SpawnParent
    from tests.db_support import run_db
    from tests.fake_claude import fake_claude

    seen = tmp_path / "seen.json"
    body = f"SEEN = {str(seen)!r}\n" + r'''
import json, os, pathlib, sys
turn = sys.stdin.readline()
pathlib.Path(SEEN).write_text(json.dumps({"argv": sys.argv, "cwd": os.getcwd(), "files": os.listdir(".")}))
model = sys.argv[sys.argv.index("--model") + 1]
print(json.dumps({"type": "system", "subtype": "init", "session_id": "synthetic", "model": model,
                  "tools": [], "mcp_servers": []}), flush=True)
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                  {"type": "text", "text": "synthetic private output"}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "synthetic private output", "session_id": "synthetic", "total_cost_usd": .004,
                  "modelUsage": {model: {"costUSD": .004}}}), flush=True)
'''
    cli = fake_claude(tmp_path / "claude", body)

    async def check(pool):
        resolved = await agent_settings.load_agent_settings(agent_settings.AgentType.MEMORY_DRAFT)
        prompt = "synthetic private vault body"
        child = ChildRun(SpawnKind.MEMORY_DRAFT, system_prompt(resolved), prompt, 120,
                         max_turns=1, role=agent_settings.AgentType.MEMORY_DRAFT, private=True)
        request = model_call.ChildCall(child, SpawnParent(None), resolved, .15, tmp_path / "runtime", cli)
        done = await model_call.call(request, parse=lambda text: text)
        assert isinstance(done, model_call.CallOk) and done.value == "synthetic private output"
        launch = json.loads(seen.read_text())
        argv = launch["argv"]
        assert argv[argv.index("--tools") + 1] == ""
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
        assert launch["files"] == []
        assert not __import__("pathlib").Path(launch["cwd"]).exists()
        row = dict(await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", done.record_id))
        assert row["run_kind"] == "memory_draft" and row["output_text"] is None
        reference = json.loads(row["input_text"])
        assert reference["chars"] == len(prompt) and len(reference["sha256"]) == 64
        events = await pool.fetch("SELECT event FROM llm_record_events WHERE record_id = $1", done.record_id)
        stored = json.dumps([row, [dict(event) for event in events]], default=str)
        assert "synthetic private vault body" not in stored
        assert "synthetic private output" not in stored

    run_db(check, monkeypatch)
