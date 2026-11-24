"""The run tree on a real PostgreSQL (the advisor RFC, sections 5 and 7): the migrations of ``llm_records``,
``task_steps``, ``agent_spawns`` and ``advisor_advice``, a spawn's state machine, and ``spawn``/``run_child`` with
a Claude Code stand-in.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from looper import child_env
from tests.db_support import DSN, fresh_connection, run_db
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

# A Claude Code that writes down how it was started and where, then answers, refuses, hangs or overspends as
# SPAWN_MODE says.
_CHILD_CLAUDE = r'''
import json, os, sys, time
mode = os.environ.get("SPAWN_MODE", "answer")
turn = json.loads(sys.stdin.readline())
flag = next(f for f in ("--system-prompt-file", "--append-system-prompt-file") if f in sys.argv)
with open(os.environ["SPAWN_SEEN"], "a") as seen:
    seen.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "files": os.listdir("."),
                           "system": open(sys.argv[sys.argv.index(flag) + 1]).read(),
                           "turn": turn["message"]["content"]}) + "\n")
def emit(event):
    print(json.dumps(event), flush=True)
model = "claude-opus-5-5"
emit({"type": "system", "subtype": "init", "session_id": "s-1", "model": model, "tools": [], "mcp_servers": []})
if mode == "hang":
    time.sleep(60)
usage = {"input_tokens": 40, "output_tokens": 6, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
text = "Use a process pool." if mode != "refuse" else ""
emit({"type": "assistant", "session_id": "s-1", "message": {"id": "m-1", "role": "assistant", "model": model,
      "stop_reason": "refusal" if mode == "refuse" else "end_turn",
      "content": [{"type": "text", "text": text}], "usage": usage}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": "s-1",
      "stop_reason": "refusal" if mode == "refuse" else "end_turn",
      "total_cost_usd": 0.5 if mode == "over_cap" else 0.02, "usage": usage,
      "modelUsage": {("claude-sonnet-5-5" if mode == "other_model" else model): {"costUSD": 0.02}}})
'''


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPAWN_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"SPAWN_SEEN", "SPAWN_MODE"})


async def _task(pool: Any, task_id: str = "task-spawn") -> tuple[str, str]:
    from looper.db import projects

    project = (await projects.create_project(name="Import", pool=pool))["id"]
    await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                       "VALUES ($1, $2, 'Speed up the import.', 'running')", task_id, project)
    await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                       "VALUES ($1, $2, 0, 's1', 'Rewrite the loop.')", f"{task_id}-s1", task_id)
    return task_id, f"{task_id}-s1"


# --- Migrations ---

_NEW_COLUMNS = (("llm_records", "parent_record_id"), ("llm_records", "run_kind"), ("task_steps", "difficulty"))
_NEW_TABLES = ("agent_spawns", "advisor_advice")


async def _shape(conn: Any) -> dict[str, Any]:
    """The columns, constraints and indexes of the tables the migrations touch."""
    tables = ["llm_records", "task_steps", *_NEW_TABLES]
    columns = await conn.fetch(
        "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ANY($1) ORDER BY table_name, column_name",
        tables)
    constraints = await conn.fetch(
        "SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def, convalidated "
        "FROM pg_constraint WHERE conrelid = ANY(SELECT to_regclass(t) FROM unnest($1::text[]) AS t) ORDER BY 1, 2",
        tables)
    indexes = await conn.fetch(
        "SELECT tablename, indexname, indexdef, i.indisvalid FROM pg_indexes "
        "JOIN pg_index i ON i.indexrelid = to_regclass(indexname) WHERE schemaname = current_schema() "
        "AND tablename = ANY($1) ORDER BY 1, 2", tables)
    return {"columns": [dict(r) for r in columns], "constraints": [dict(r) for r in constraints],
            "indexes": [{**dict(r), "indexdef": r["indexdef"].split(" ON ", 1)[1]} for r in indexes]}


async def _before_the_run_tree(conn: Any) -> None:
    """Before the run tree and its dependent explorer, failure-typing and attempt migrations."""
    # Roll back the dependent explorer migration too, so recreating this older
    # table exercises the complete forward migration path.
    await conn.execute("DELETE FROM schema_migrations WHERE name IN "
                       "('platform_subagents', 'platform_subagents_online')")
    await conn.execute("ALTER TABLE projects DROP COLUMN subagents, DROP COLUMN subagent_budget_usd, "
                       "DROP COLUMN subagent_task_budget_usd, DROP COLUMN subagents_per_run, "
                       "DROP COLUMN subagents_per_task")
    await conn.execute("DROP TABLE advisor_advice, agent_spawns")
    await conn.execute("ALTER TABLE llm_records DROP COLUMN parent_record_id, DROP COLUMN run_kind, "
                       "DROP COLUMN failure_class, DROP COLUMN failure_cause, DROP COLUMN attempt_id")
    await conn.execute("ALTER TABLE task_steps DROP COLUMN difficulty, DROP COLUMN checks")
    await conn.execute("ALTER TABLE projects DROP COLUMN acceptance_checks")
    await conn.execute("DELETE FROM schema_migrations WHERE name = ANY($1)", [
        "run_tree", "agent_spawns", "step_difficulty", "run_tree_online", "step_difficulty_online",
        "failure_typing", "failure_typing_online", "acceptance_checks", "attempt_ids", "attempt_ids_online"])


def test_a_database_from_before_the_run_tree_gets_it_without_a_row_rewritten_and_with_the_fresh_shape() -> None:
    from looper.db import connection
    from looper.db.llm_records import RUN_KIND

    async def main() -> None:
        async with fresh_connection(command_timeout=60) as conn:
            await connection.ensure_schema(conn)
            fresh = await _shape(conn)
            await _before_the_run_tree(conn)
            # The upgrade must start before both dependent features, not with a
            # newer failure-typing schema hidden under an older run-tree marker.
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND table_name='llm_records' AND column_name IN "
                "('parent_record_id', 'run_kind', 'failure_class', 'failure_cause'))")
            assert not await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE name IN "
                "('failure_typing', 'failure_typing_online', 'platform_subagents', 'platform_subagents_online'))")
            agents = ["manager", "manager-ops", "planner-t1", "worker-0a1b2c3d4e5f", "reviewer-0a1b2c3d4e5f",
                      "processor:sched-1", "consult-0a1b2c3d4e5f", "gate-run-1", "managerx", "planner_x"]
            for agent in agents:
                await conn.execute("INSERT INTO llm_records (agent_id) VALUES ($1)", agent)
            rows = "SELECT id, ctid::text, xmin::text FROM llm_records ORDER BY id"
            before = [tuple(r) for r in await conn.fetch(rows)]
            assert await connection.schema_change_pending(conn)
            await connection.ensure_schema(conn)
            assert not await connection.schema_change_pending(conn)
            # No row was rewritten: each keeps its place and the transaction that wrote it.
            assert [tuple(r) for r in await conn.fetch(rows)] == before
            assert await conn.fetchval("SELECT count(*) FROM llm_records WHERE run_kind IS NOT NULL "
                                       "OR parent_record_id IS NOT NULL") == 0
            # Readers take an older row's kind from its agent id.
            kinds = {r["agent_id"]: r["kind"] for r in await conn.fetch(f"SELECT agent_id, {RUN_KIND} AS kind "
                                                                           "FROM llm_records")}
            assert kinds == {"manager": "manager", "manager-ops": "manager", "planner-t1": "planner",
                             "worker-0a1b2c3d4e5f": "worker", "reviewer-0a1b2c3d4e5f": "reviewer",
                             "processor:sched-1": "processor", "consult-0a1b2c3d4e5f": "consult",
                             "gate-run-1": None, "managerx": None, "planner_x": None}
            markers = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            assert {"run_tree", "agent_spawns", "step_difficulty", "run_tree_online",
                    "step_difficulty_online"} <= markers
            assert await _shape(conn) == fresh  # its constraints validated and its index valid, as a fresh one's
            # A marked database is never repaired: dropping a column again leaves it dropped.
            await conn.execute("ALTER TABLE task_steps DROP COLUMN difficulty")
            await connection.ensure_schema(conn)
            assert ("task_steps", "difficulty") not in {
                (r["table_name"], r["column_name"]) for r in (await _shape(conn))["columns"]}

    asyncio.run(main())


def test_a_start_stopped_after_the_run_tree_committed_finishes_it_on_the_next_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import connection

    async def stopped(conn: Any) -> None:
        return None

    async def main() -> None:
        async with fresh_connection(command_timeout=60) as conn:
            await connection.ensure_schema(conn)
            fresh = await _shape(conn)
            await _before_the_run_tree(conn)
            with monkeypatch.context() as patch:  # the start stops once the schema transaction committed
                patch.setattr(connection, "_finish_online", stopped)
                await connection.ensure_schema(conn)
            shape = await _shape(conn)
            assert not {c["conname"]: c["convalidated"] for c in shape["constraints"]}[
                "llm_records_run_kind_check"]
            assert "idx_llm_records_parent" not in {i["indexname"] for i in shape["indexes"]}
            assert not {"run_tree_online", "step_difficulty_online"} & {
                r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            # A concurrent build that stopped leaves an invalid index (here a unique one two children break); the
            # next start drops and rebuilds it.
            parent = await conn.fetchval("INSERT INTO llm_records (agent_id) VALUES ('planner-t1') RETURNING id")
            for child in ("advisor-0a1b2c3d4e5f", "advice_check-0a1b2c3d4e5f"):
                await conn.execute("INSERT INTO llm_records (agent_id, parent_record_id) VALUES ($1, $2)", child,
                                   parent)
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute("CREATE UNIQUE INDEX CONCURRENTLY idx_llm_records_parent "
                                   "ON llm_records(parent_record_id) WHERE parent_record_id IS NOT NULL")
            assert not await conn.fetchval("SELECT indisvalid FROM pg_index "
                                           "WHERE indexrelid = to_regclass('idx_llm_records_parent')")
            await connection.ensure_schema(conn)
            assert await _shape(conn) == fresh

    asyncio.run(main())


def test_a_fresh_database_has_the_run_tree_without_recording_its_upgrades() -> None:
    from looper.db import connection

    async def main() -> None:
        async with fresh_connection(command_timeout=60) as conn:
            await connection.ensure_schema(conn)
            shape = await _shape(conn)
            columns = {(r["table_name"], r["column_name"]) for r in shape["columns"]}
            assert set(_NEW_COLUMNS) <= columns and {t for t, _ in columns} >= set(_NEW_TABLES)
            markers = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            assert not {"run_tree", "agent_spawns", "step_difficulty"} & markers

    asyncio.run(main())


# --- Records ---


def test_a_record_takes_its_kind_from_its_agent_id_unless_given_and_names_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import llm_records

    async def test(pool: Any) -> None:
        planner = await llm_records.start_record(agent_id="planner-t1", task_id=None)
        child = await llm_records.start_record(agent_id="advisor-0a1b2c3d4e5f", run_kind="advisor",
                                               parent_record_id=planner)
        other = await llm_records.start_record(agent_id="gate-run-1")
        rows = {r["id"]: dict(r) for r in await pool.fetch("SELECT id, run_kind, parent_record_id FROM llm_records")}
        assert rows[planner]["run_kind"] == "planner" and rows[planner]["parent_record_id"] is None
        assert (rows[child]["run_kind"], rows[child]["parent_record_id"]) == ("advisor", planner)
        assert rows[other]["run_kind"] is None
        with pytest.raises(ValueError, match="unknown run kind"):
            await llm_records.start_record(agent_id="x-1", run_kind="unknown")
        # A parent's record going away keeps its children, without the link.
        await pool.execute("DELETE FROM llm_records WHERE id = $1", planner)
        assert await pool.fetchval("SELECT parent_record_id FROM llm_records WHERE id = $1", child) is None

    run_db(test, monkeypatch)


# --- A spawn's states ---


def test_a_spawn_moves_only_along_its_transitions_and_from_the_state_its_caller_expects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.db import agent_spawns
    from looper.db.agent_spawns import SpawnState, TransitionError

    async def test(pool: Any) -> None:
        task, step = await _task(pool)
        row = await agent_spawns.create(kind="advisor", task_id=task, step_id=step, parent_record_id=None,
                                        brief={"source": "checkpoint"}, inputs={"request": {"v": "advice_request/1"}},
                                        budget_usd=1.0)
        assert (row["state"], row["rounds"], row["returns"], row["finished_at"]) == ("drafting", 0, 0, None)
        sid = row["id"]
        row = await agent_spawns.move(sid, SpawnState.LINTING, expect=SpawnState.DRAFTING)
        row = await agent_spawns.move(sid, SpawnState.DRAFTING, expect=SpawnState.LINTING, returned=True,
                                      append={"checks": {"return": 1, "stage": "code", "category": "local_path",
                                                         "field": "problem", "start": 4, "end": 30}})
        assert row["returns"] == 1 and row["result"]["checks"][0]["category"] == "local_path"
        # Another caller moved it first: nothing changes.
        assert await agent_spawns.move(sid, SpawnState.ASKING, expect=SpawnState.LINTING) is None
        await agent_spawns.move(sid, SpawnState.LINTING, expect=SpawnState.DRAFTING)
        row = await agent_spawns.move(sid, SpawnState.ASKING, expect=SpawnState.LINTING, spent_usd=0.0004,
                                      result={"judge": {"answerable": 0.8}})
        assert row["rounds"] == 1 and row["spent_usd"] == pytest.approx(0.0004)
        row = await agent_spawns.move(sid, SpawnState.NEEDS_INFO, expect=SpawnState.ASKING, spent_usd=0.05,
                                      append={"rounds": {"record_id": None, "cost_usd": 0.05}})
        await agent_spawns.move(sid, SpawnState.LINTING, expect=SpawnState.NEEDS_INFO)
        await agent_spawns.move(sid, SpawnState.ASKING, expect=SpawnState.LINTING)
        row = await agent_spawns.move(sid, SpawnState.ANSWERED, expect=SpawnState.ASKING, spent_usd=0.04,
                                      append={"rounds": {"record_id": None, "cost_usd": 0.04}})
        assert row["state"] == "answered" and row["finished_at"] is not None and row["rounds"] == 2
        assert [r["cost_usd"] for r in row["result"]["rounds"]] == [0.05, 0.04]
        assert row["result"]["judge"] == {"answerable": 0.8} and len(row["result"]["checks"]) == 1
        assert row["spent_usd"] == pytest.approx(0.0904)
        # A finished spawn never moves again, and a move the machine lacks is a caller's mistake.
        with pytest.raises(TransitionError):
            await agent_spawns.move(sid, SpawnState.ASKING, expect=SpawnState.ANSWERED)
        with pytest.raises(TransitionError):
            await agent_spawns.move(sid, SpawnState.ANSWERED, expect=SpawnState.DRAFTING)
        with pytest.raises(TransitionError):
            await agent_spawns.move(sid, SpawnState.FAILED, expect=SpawnState.ASKING, returned=True)
        with pytest.raises(TransitionError):
            await agent_spawns.create(kind="advisor", task_id=task, step_id=None, parent_record_id=None, brief={},
                                      inputs={}, budget_usd=1.0, state=SpawnState.ANSWERED)
        with pytest.raises(ValueError, match="unknown spawn kind"):
            await agent_spawns.create(kind="unknown", task_id=task, step_id=None, parent_record_id=None, brief={},
                                      inputs={}, budget_usd=1.0)

    run_db(test, monkeypatch)


def test_the_spawn_tables_refuse_what_their_states_do_not_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    async def test(pool: Any) -> None:
        task, step = await _task(pool)
        insert = ("INSERT INTO agent_spawns (task_id, step_id, kind, brief, inputs, budget_usd, state, returns, "
                  "finished_at) VALUES ($1, $2, 'advisor', '{}', '{}', 1, $3, $4, $5) RETURNING id")
        for state, returns, finished in (("answered", 0, None), ("asking", 0, "now"), ("drafting", 3, None)):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.fetchval(insert, task, step, state, returns,
                                    None if finished is None else await pool.fetchval("SELECT now()"))
        spawn = await pool.fetchval(insert, task, step, "asking", 0, None)
        advice = ("INSERT INTO advisor_advice (spawn_id, task_id, step_id, attempt, source, kind, question, advice, "
                  "adoption, adopted_at) VALUES ($1, $2, $3, $4, $5, 'plan_review', '{}', $6, $7, $8)")
        now = await pool.fetchval("SELECT now()")
        for attempt, source, answer, adoption, at in ((0, "checkpoint", None, None, None),
                                                      (1, "explorer", None, None, None),
                                                      (1, "checkpoint", None, "adopted", now),
                                                      (1, "checkpoint", "{}", "adopted", None)):
            with pytest.raises(asyncpg.CheckViolationError):
                await pool.execute(advice, spawn, task, step, attempt, source, answer, adoption, at)
        await pool.execute(advice, spawn, task, step, 1, "checkpoint", "{}", "partial", now)
        # The step going away keeps the consult and its advice for the task's record.
        await pool.execute("DELETE FROM task_steps WHERE id = $1", step)
        assert await pool.fetchval("SELECT step_id FROM advisor_advice WHERE spawn_id = $1", spawn) is None

    run_db(test, monkeypatch)


# --- spawn and run_child ---


async def _spawn(pool: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, mode: str = "answer",
                 budget: float = 1.0, timeout: float = 20, settings: Any = None) -> Any:
    from looper.agents import spawn
    from looper.agents.agent_settings import load_agent_settings
    from looper.agents.system_prompt import SystemPrompt
    from looper.db import llm_records

    monkeypatch.setenv("SPAWN_MODE", mode)
    task, step = await _task(pool)
    parent = await llm_records.start_record(agent_id=f"planner-{task}", task_id=task)
    settings = settings or await load_agent_settings("worker", None, task)
    child = spawn.ChildRun(spawn.SpawnKind.ADVISOR, SystemPrompt("ADVISOR CONTRACT", {}),
                           '{"v": "advice_request/1"}', timeout_s=timeout)
    outcome = await spawn.spawn(spawn.SpawnKind.ADVISOR, spawn.SpawnParent(task, step, parent),
                                {"source": "optional"}, {"request": {"v": "advice_request/1"}}, settings, budget,
                                child=child, runtime_root=tmp_path / "runtime",
                                claude_bin=fake_claude(tmp_path / "claude", _CHILD_CLAUDE))
    return outcome, child, parent


def _seen(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "seen.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_a_spawn_runs_one_child_with_no_tools_in_an_empty_temporary_directory_and_records_it_under_its_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        outcome, child, parent = await _spawn(pool, tmp_path, monkeypatch)
        assert (outcome.child.output, outcome.child.failure, outcome.child.cost_usd) == ("Use a process pool.", None,
                                                                                         0.02)
        row = outcome.row
        assert (row["state"], row["rounds"], row["child_record_id"]) == ("answered", 1, outcome.child.record_id)
        assert row["result"]["output"] == "Use a process pool." and row["spent_usd"] == pytest.approx(0.02)
        assert row["result"]["rounds"] == [{"record_id": outcome.child.record_id, "cost_usd": 0.02,
                                            "models": ["claude-opus-5-5"], "failure": None}]
        (seen,) = _seen(tmp_path)
        # Its directory: empty, under the system's temporary directory, named for the run, and gone afterwards.
        directory = Path(tempfile.gettempdir()) / "looper-advisor" / child.run_id
        assert Path(seen["cwd"]).resolve() == directory.resolve() and seen["files"] == []
        assert not directory.exists()
        argv = seen["argv"]
        assert argv[argv.index("--tools") + 1] == "" and "--disable-slash-commands" in argv
        assert "--system-prompt-file" in argv and seen["system"] == "ADVISOR CONTRACT"
        assert argv[argv.index("--max-turns") + 1] == "2" and argv[argv.index("--max-budget-usd") + 1] == "1"
        assert argv[argv.index("--setting-sources") + 1] == "" and "--dangerously-skip-permissions" not in argv
        assert seen["turn"] == '{"v": "advice_request/1"}'
        record = dict(await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", outcome.child.record_id))
        assert (record["agent_id"], record["run_kind"], record["parent_record_id"]) == (child.agent_id, "advisor",
                                                                                         parent)
        assert record["task_id"] == "task-spawn" and json.loads(record["metadata"])["step_id"] == "task-spawn-s1"

    run_db(test, monkeypatch)


@pytest.mark.parametrize(("model", "answered", "refusal"), [
    ("claude-opus-5-5", {"claude-opus-5-5": {}}, None),
    ("claude-opus-5-5", {"claude-sonnet-5-5": {}}, "another model answered (claude-sonnet-5-5)"),
    # an alias is the model it runs, never a pass for any answer
    ("opus", {"claude-opus-5-5-20261001": {}}, None),
    ("opus", {"claude-sonnet-5-5": {}}, "another model answered (claude-sonnet-5-5)"),
    ("haiku", {"claude-haiku-5-5": {}, "claude-opus-5-5": {}}, "another model answered (claude-opus-5-5)"),
])
def test_an_answer_is_the_models_only_when_that_model_alone_gave_it(
    model: str, answered: dict[str, Any], refusal: str | None,
) -> None:
    from looper.agents.spawn import _refusal

    assert _refusal({"stop_reason": "end_turn", "modelUsage": answered}, model) == refusal


@pytest.mark.parametrize(("mode", "state", "failure"), [
    ("refuse", "failed", "refused"),
    ("other_model", "failed", "refused"),
    ("over_cap", "over_budget", "budget"),
])
def test_a_spawn_whose_child_refused_or_overspent_keeps_no_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, state: str, failure: str,
) -> None:
    from looper.agents.agent_settings import load_agent_settings
    from looper.agents.profile_registry import builtin_resolved_profile

    async def test(pool: Any) -> None:
        settings = None
        if mode == "other_model":
            # A profile naming its model in full: an answer from another model is not its answer.
            settings = await load_agent_settings("worker")
            profile = builtin_resolved_profile()
            resolved = profile.resolved_model
            from dataclasses import replace

            resolved = replace(resolved, model=replace(resolved.model, name="claude-opus-5-5"))
            settings = replace(settings, profile=replace(profile, resolved_model=resolved))
        outcome, _, _ = await _spawn(pool, tmp_path, monkeypatch, mode=mode, settings=settings, budget=0.2)
        assert outcome.child.output is None and outcome.child.failure.value == failure
        assert outcome.row["state"] == state and "output" not in outcome.row["result"]
        assert outcome.row["result"]["failure"] == failure and outcome.row["detail"]

    run_db(test, monkeypatch)


def test_a_spawn_with_too_little_budget_left_starts_no_process(monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        outcome, _, _ = await _spawn(pool, tmp_path, monkeypatch, budget=0.05)
        assert (outcome.row["state"], outcome.child.failure.value, outcome.child.record_id) == ("over_budget", "budget",
                                                                                                None)
        assert "less than the 0.10 USD" in outcome.row["detail"] and _seen(tmp_path) == []

    run_db(test, monkeypatch)


def test_a_spawn_over_its_deadline_is_ended_with_its_directory_removed(monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path: Path) -> None:
    async def test(pool: Any) -> None:
        outcome, child, _ = await _spawn(pool, tmp_path, monkeypatch, mode="hang", timeout=2)
        assert (outcome.row["state"], outcome.child.failure.value) == ("failed", "timeout")
        assert not (Path(tempfile.gettempdir()) / "looper-advisor" / child.run_id).exists()

    run_db(test, monkeypatch, timeout=60)


def test_a_spawn_whose_runtime_has_no_mode_without_tools_does_not_start(monkeypatch: pytest.MonkeyPatch,
                                                                        tmp_path: Path) -> None:
    from dataclasses import replace

    from looper.agents.agent_settings import load_agent_settings
    from looper.agents.runtimes import Capability

    async def test(pool: Any) -> None:
        from looper.agents import runtimes

        settings = await load_agent_settings("worker")
        claude = runtimes.ADAPTERS["claude-code"]
        monkeypatch.setattr(type(claude), "capabilities",
                            property(lambda self: frozenset(c for c in Capability if c is not Capability.NO_TOOLS)))
        outcome, _, _ = await _spawn(pool, tmp_path, monkeypatch, settings=replace(settings))
        assert (outcome.row["state"], outcome.child.failure.value) == ("failed", "unavailable")
        assert "no mode without tools" in outcome.row["detail"] and _seen(tmp_path) == []

    run_db(test, monkeypatch)
