"""Agent setting layers, their one-time migration and role-prompt seeding against a real PostgreSQL.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database. Each
test works in a new schema of its own, dropped afterwards.

The migration's parity is checked against ``fixtures/agent_settings_parity_main.json``:
for each stored scenario (the values the old columns and Settings keys held),
what the code before the layers (``main`` at 72e72eb65) produced for every
role: its launch flags, environment and system prompt. It was generated once
from that code's own resolution (``agent_settings.defaults_from``,
``resolve_harness``, ``resolve_launch`` and ``ProjectExecution``), so it is not
derived from the code under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

DSN = os.environ.get("LOOPER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

PARITY = json.loads((Path(__file__).parent / "fixtures" / "agent_settings_parity_main.json").read_text("utf-8"))


@asynccontextmanager
async def _fresh_schema() -> AsyncIterator[Any]:
    import asyncpg

    schema = f"looper_test_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3, server_settings={"search_path": schema})
    try:
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


def _run(test: Callable[[Any], Awaitable[None]], monkeypatch: pytest.MonkeyPatch | None = None, *,
         before: Callable[[Any], Awaitable[None]] | None = None) -> None:
    """Run ``test(pool)`` on a new schema (``before(pool)`` runs ahead of it); with ``monkeypatch`` the pool
    is also the server's pool."""
    from looper.db import connection

    async def _main() -> None:
        async with _fresh_schema() as pool:
            if before is not None:
                await before(pool)
            await connection.ensure_schema(pool)
            if monkeypatch is not None:
                monkeypatch.setattr(connection, "_pool", pool)
            await test(pool)

    asyncio.run(_main())


# --- The upgrade: the old columns' values move into their layers, and every role launches as before ---

# The tables as they were before the layers.
_BEFORE_LAYERS = """
CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL,
                              updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE agent_configs (
    agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT '',
    runtime TEXT NOT NULL DEFAULT 'claude-code', enabled BOOLEAN NOT NULL DEFAULT true,
    system_prompt TEXT NOT NULL DEFAULT '', model TEXT, max_turns INTEGER NOT NULL DEFAULT 0,
    timeout_seconds INTEGER NOT NULL DEFAULT 1200, max_concurrency INTEGER NOT NULL DEFAULT 1, extra_args JSONB,
    metadata JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    default_prompt TEXT NOT NULL DEFAULT '', harness_prompt TEXT NOT NULL DEFAULT '');
CREATE TABLE projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', work_dir TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    background TEXT NOT NULL DEFAULT '', planner_harness_prompt TEXT NOT NULL DEFAULT '',
    worker_harness_prompt TEXT NOT NULL DEFAULT '', model TEXT,
    run_budget_usd DOUBLE PRECISION CHECK (run_budget_usd > 0));
"""


def _old_state(scenario: dict[str, Any]) -> Callable[[Any], Awaitable[None]]:
    async def before(pool: Any) -> None:
        await pool.execute(_BEFORE_LAYERS)
        rows = [*scenario["system_settings"].items(), ("catalog.last_import", "kept")]
        await pool.executemany("INSERT INTO system_settings (key, value) VALUES ($1, $2)", rows)
        await pool.executemany(
            "INSERT INTO agent_configs (agent_id, role, harness_prompt, model) VALUES ($1, $1, $2, $3)",
            [(agent_id, row["harness_prompt"], row["model"]) for agent_id, row in scenario["agent_configs"].items()])
        await pool.executemany(
            "INSERT INTO projects (id, name, background, model, run_budget_usd, planner_harness_prompt, "
            "worker_harness_prompt) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            [(project_id, p["name"], p["background"], p["model"], p["run_budget_usd"], p["planner_harness_prompt"],
              p["worker_harness_prompt"]) for project_id, p in scenario["projects"].items()])

    return before


_ENV = [{"key": "ZED", "value": "1"}, {"key": "ALPHA", "value": "two words"}]
# Each scenario's layers after the upgrade: (scope, role, project, harness, agent, effort, env, cap); each model
# became the agent claude-code/<model> (the agents' upgrade, which follows this one).
_EXPECTED_LAYERS = {
    "configured": [
        ("project", None, "proj-a", None, "claude-code/opus[1m]", None, [], 0.5),
        ("role", "manager", None, "Manager harness HM.\n", "claude-code/opus", None, [], None),
        ("role", "planner", None, None, "claude-code/haiku", None, [], None),
        ("role", "worker", None, None, "claude-code/claude-sonnet-4-5", None, [], None),
        ("role_project", "planner", "proj-a", "\nPlan harness PH.", None, None, [], None),
        ("role_project", "worker", "proj-a", "Work harness WH.  ", None, None, [], None),
        ("system", None, None, "\n\n  System harness H0.\nSecond line.  \n\n", "claude-code/sonnet", "high", _ENV,
         None),
    ],
    "builtin_text_and_empty": [
        ("project", None, "proj-a", None, None, None, [], 0.25),
        ("role_project", "worker", "proj-a", "Work only.", None, None, [], None),
        ("system", None, None, PARITY["scenarios"]["builtin_text_and_empty"]["system_settings"]["agent.harness_prompt"],
         None, None, [], None),
    ],
}


async def _layers(pool: Any) -> list[tuple[Any, ...]]:
    rows = await pool.fetch(
        "SELECT l.scope, l.role, l.project_id, l.harness_prompt, a.name, l.effort, l.env, l.run_budget_usd "
        "FROM agent_setting_layers l LEFT JOIN agents a ON a.id = l.agent_id "
        "ORDER BY l.scope, l.role NULLS FIRST, l.project_id NULLS FIRST")
    return [(*tuple(r)[:6], json.loads(r["env"]), r["run_budget_usd"]) for r in rows]


@pytest.mark.parametrize("name", sorted(PARITY["scenarios"]))
def test_the_upgrade_moves_every_value_into_its_layer_and_each_role_launches_as_before(
    monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    from looper.agents.agent_settings import Launch, load_agent_settings
    from looper.agents.system_prompt import build_system_prompt
    from looper.db import connection

    scenario, golden = PARITY["scenarios"][name], PARITY["cases"][name]

    async def _test(pool: Any) -> None:
        assert await _layers(pool) == _EXPECTED_LAYERS[name]
        # The old columns and keys are gone; another key stays.
        columns = {(r["table_name"], r["column_name"]) for r in await pool.fetch(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name IN ('agent_configs', 'projects')")}
        assert not columns & {("agent_configs", "harness_prompt"), ("agent_configs", "model"),
                              ("projects", "planner_harness_prompt"), ("projects", "worker_harness_prompt"),
                              ("projects", "model"), ("projects", "run_budget_usd")}
        assert {r["key"] for r in await pool.fetch("SELECT key FROM system_settings")} == {"catalog.last_import"}
        # Applying the schema again changes nothing.
        before = await _layers(pool)
        await connection.ensure_schema(pool)
        assert await _layers(pool) == before

        # Every role launches with the flags, environment and system prompt the code before the layers gave.
        for case, expected in golden.items():
            role, _, project_id = case.partition("@")
            settings = await load_agent_settings(role, project_id or None)
            project = {"id": project_id, **scenario["projects"][project_id]} if project_id else None
            launch = Launch(settings)
            prompt = build_system_prompt(harness=settings.harness, role_id=role, role="ROLE.", recommended=True,
                                         contract_name="CONTRACT", contract="CONTRACT.", project=project,
                                         task="TASK." if project else "")
            assert {"args": launch.args(), "env": [list(p) for p in launch.env], "env_sha256": launch.env_sha256,
                    "system_prompt": prompt.text} == expected, case

    _run(_test, monkeypatch, before=_old_state(scenario))


def test_a_fresh_database_has_no_layers_and_every_role_runs_with_the_builtin_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.agent_settings import BUILTIN_SETTINGS, load_agent_settings

    async def _test(pool: Any) -> None:
        assert await _layers(pool) == []
        assert await pool.fetchval("SELECT 1 FROM schema_migrations WHERE name = 'agent_setting_layers'")
        for role in ("manager", "planner", "worker", "reviewer"):
            assert await load_agent_settings(role, "proj-x") == BUILTIN_SETTINGS

    _run(_test, monkeypatch)


# --- The store ---


def test_each_layer_round_trips_and_a_deleted_project_takes_its_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.agents import agent_settings
    from looper.db import agent_setting_layers, projects

    async def _test(pool: Any) -> None:
        calc = await projects.create_project(name="calc", pool=pool)
        pid = calc["id"]
        await agent_settings.update_system({"agent": "agent-claude-code-opus", "env": [
            {"key": "TZ", "value": "UTC"}, {"key": "LANG", "value": "C"}]})
        await agent_settings.update_role("reviewer", {"effort": "max"})
        await agent_settings.update_project(pid, {"run_budget_usd": 0.5, "env": [{"key": "TZ", "value": None}]})
        view = await agent_settings.update_project(pid, {"harness_prompt": "W."}, role="worker")
        reviewer = view["roles"]["reviewer"]["effective"]
        assert (reviewer["effort"], reviewer["run_budget_usd"], reviewer["env"]) == (
            {"value": "max", "source": "role"}, {"value": 0.5, "source": "project"},
            [{"key": "LANG", "value": "C", "source": "system"}])
        assert reviewer["harness_prompt"] == {"value": "W.", "source": "role_project", "from_role": "worker"}
        # The order of the variables survives the round trip (the hash of the environment depends on it).
        system = (await agent_settings.settings_view())["system"]["values"]
        assert [e["key"] for e in system["env"]] == ["TZ", "LANG"]

        # Clearing every field of a layer removes its row; the table refuses what validation would.
        await agent_settings.update_project(pid, {"run_budget_usd": None, "env": []})
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE scope = 'project'") == 0
        with pytest.raises(asyncpg.CheckViolationError):
            await agent_setting_layers.put_layer("role_project", "manager", pid, {"effort": "low"}, pool=pool)
        with pytest.raises(asyncpg.CheckViolationError):
            await agent_setting_layers.put_layer("system", "worker", None, {"effort": "low"}, pool=pool)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await agent_setting_layers.put_layer("system", None, None, {"agent_id": "agent-nowhere"}, pool=pool)

        await projects.delete_project(pid, pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE project_id IS NOT NULL") == 0

    _run(_test, monkeypatch)


def test_seeding_moves_unedited_role_prompts_keeps_edited_ones_and_adds_the_reviewer() -> None:
    from looper.agents.serve import seed_agent_configs
    from looper.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT as MANAGER
    from looper.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT as PLANNER
    from looper.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT as WORKER, REVIEWER_SYSTEM_PROMPT as REVIEWER
    from looper.db import agent_configs

    async def _test(pool: Any) -> None:
        # Rows as the previous release left them: manager and worker unedited, planner edited.
        for agent_id, prompt, recommended in [
            ("manager", "previous manager recommendation", "previous manager recommendation"),
            ("planner", "My own planner prompt.", "previous planner recommendation"),
            ("worker", "previous worker recommendation", "previous worker recommendation"),
        ]:
            await pool.execute(
                "INSERT INTO agent_configs (agent_id, role, system_prompt, default_prompt, max_turns) "
                "VALUES ($1, $1, $2, $3, $4)", agent_id, prompt, recommended, 7 if agent_id == "worker" else 0,
            )
        await seed_agent_configs(pool)

        rows = {agent_id: await agent_configs.get_config(agent_id, pool=pool)
                for agent_id in ("manager", "planner", "worker", "reviewer")}
        assert (rows["manager"]["system_prompt"], rows["manager"]["default_prompt"]) == (MANAGER, MANAGER)
        assert (rows["worker"]["system_prompt"], rows["worker"]["default_prompt"]) == (WORKER, WORKER)
        assert rows["worker"]["max_turns"] == 7  # other fields stay
        assert (rows["planner"]["system_prompt"], rows["planner"]["default_prompt"]) == (
            "My own planner prompt.", PLANNER)
        reviewer = rows["reviewer"]
        assert (reviewer["system_prompt"], reviewer["default_prompt"], reviewer["role"]) == (
            REVIEWER, REVIEWER, "reviewer")
        assert (reviewer["timeout_seconds"], reviewer["max_turns"]) == (86400, 0)
        assert not {"harness_prompt", "model", "runtime", "llm_profile_id"} & set(reviewer)  # settings are layers
        # The new recommendations hold responsibilities only: character and stability moved to the harness.
        assert "Default to Chinese" not in MANAGER and "Be concise" not in MANAGER
        assert "Answer the user in their language" not in PLANNER
        assert "Check by running things" not in WORKER

        # A second startup changes nothing.
        before = await pool.fetch("SELECT agent_id, system_prompt, default_prompt FROM agent_configs ORDER BY 1")
        await seed_agent_configs(pool)
        assert await pool.fetch("SELECT agent_id, system_prompt, default_prompt FROM agent_configs ORDER BY 1") == before

    _run(_test)
