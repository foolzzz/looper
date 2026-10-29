"""Current agent setting layers and role-prompt seeding on disposable PostgreSQL."""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


async def _layers(pool: Any) -> list[tuple[Any, ...]]:
    rows = await pool.fetch(
        "SELECT l.scope, l.role, l.project_id, l.harness_prompt, a.name, l.env, l.run_budget_usd "
        "FROM agent_setting_layers l LEFT JOIN agent_profiles a ON a.id = l.profile_id "
        "ORDER BY l.scope, l.role NULLS FIRST, l.project_id NULLS FIRST")
    return [(*tuple(r)[:5], json.loads(r["env"]), r["run_budget_usd"]) for r in rows]


def test_a_fresh_database_has_no_layers_and_every_role_runs_with_the_builtin_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from looper.agents.agent_settings import BUILTIN_SETTINGS, load_agent_settings
    from looper.agents.profile_registry import builtin_resolved_profile

    async def _test(pool: Any) -> None:
        assert await _layers(pool) == []
        for role in ("manager", "planner", "worker", "reviewer"):
            from dataclasses import replace

            from looper.agents.agent_settings import BUILTIN_SYSTEM_PROMPT_MODE, AgentType

            assert await load_agent_settings(role, "proj-x") == replace(
                BUILTIN_SETTINGS, profile=builtin_resolved_profile(),
                system_prompt_mode=BUILTIN_SYSTEM_PROMPT_MODE[AgentType(role)])

    run_db(_test, monkeypatch)


# --- The store ---


def test_each_layer_round_trips_and_a_deleted_project_takes_its_layers(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg

    from looper.agents import agent_settings, profile_registry
    from looper.db import agent_setting_layers, projects

    async def _test(pool: Any) -> None:
        calc = await projects.create_project(name="calc", pool=pool)
        pid = calc["id"]
        await agent_settings.update_system({"profile_id": "agent-claude-code-opus", "env": [
            {"key": "TZ", "value": "UTC"}, {"key": "LANG", "value": "C"}]})
        opus = next(m for m in (await profile_registry.list_models(provider_id="prov-claude-subscription"))["models"]
                    if m["name"] == "opus")
        opus_max = (await profile_registry.create_profile({
            "name": "opus max", "runtime_id": "rt-claude-code", "model_id": opus["model_id"],
            "effort": "max"}))["profile"]["profile_id"]
        await agent_settings.update_role("reviewer", {"profile_id": opus_max})
        await agent_settings.update_project(pid, {"run_budget_usd": 0.5, "env": [{"key": "TZ", "value": None}]})
        view = await agent_settings.update_project(pid, {"harness_prompt": "W."}, role="worker")
        reviewer = view["roles"]["reviewer"]["effective"]
        assert (reviewer["effort"], reviewer["run_budget_usd"], reviewer["env"]) == (
            {"value": "max", "source": "profile"}, {"value": 0.5, "source": "project"},
            [{"key": "LANG", "value": "C", "source": "system"}])
        assert reviewer["harness_prompt"] == {"value": "W.", "source": "role_project", "from_role": "worker"}
        # The order of the variables survives the round trip (the hash of the environment depends on it).
        system = (await agent_settings.settings_view())["system"]["values"]
        assert [e["key"] for e in system["env"]] == ["TZ", "LANG"]

        # A concurrency limit on the Workers' role layer, the default for every task; one alone keeps its row.
        await agent_settings.update_role("worker", {"max_concurrency": 3})
        assert [await agent_settings.load_task_limit(agent_settings.AgentType(role), None)
                for role in ("worker", "reviewer")] == [3, 8]
        await agent_settings.update_role("worker", {"max_concurrency": None})
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE scope = 'role'") == 1  # reviewer

        # Clearing every field of a layer removes its row; the table refuses what validation would.
        await agent_settings.update_project(pid, {"run_budget_usd": None, "env": []})
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE scope = 'project'") == 0
        with pytest.raises(asyncpg.CheckViolationError):
            await agent_setting_layers.put_layer("role_project", "manager", pid, {"harness_prompt": "x"}, pool=pool)
        with pytest.raises(asyncpg.CheckViolationError):
            await agent_setting_layers.put_layer("system", "worker", None, {"harness_prompt": "x"}, pool=pool)
        for scope, role, project_id, limit in (("system", None, None, 2), ("role", "manager", None, 2),
                                               ("role", "planner", None, 2), ("project", None, pid, 2),
                                               ("role_project", "worker", pid, 2), ("role", "worker", None, 65)):
            with pytest.raises(asyncpg.CheckViolationError):
                await agent_setting_layers.put_layer(scope, role, project_id, {"max_concurrency": limit}, pool=pool)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await agent_setting_layers.put_layer("system", None, None, {"profile_id": "agent-nowhere"}, pool=pool)

        await projects.delete_project(pid, pool=pool)
        assert await pool.fetchval("SELECT count(*) FROM agent_setting_layers WHERE project_id IS NOT NULL") == 0

    run_db(_test, monkeypatch)


def test_seeding_moves_unedited_role_prompts_keeps_edited_ones_and_adds_the_reviewer() -> None:
    from looper.agents.serve import seed_role_configs
    from looper.agents.tiers.manager import DEFAULT_SYSTEM_PROMPT as MANAGER
    from looper.agents.tiers.planner import DEFAULT_SYSTEM_PROMPT as PLANNER
    from looper.agents.tiers.worker import DEFAULT_SYSTEM_PROMPT as WORKER, REVIEWER_SYSTEM_PROMPT as REVIEWER
    from looper.db import role_configs

    async def _test(pool: Any) -> None:
        # Rows as the previous release left them: manager and worker unedited, planner edited.
        for role, prompt, recommended in [
            ("manager", "previous manager recommendation", "previous manager recommendation"),
            ("planner", "My own planner prompt.", "previous planner recommendation"),
            ("worker", "previous worker recommendation", "previous worker recommendation"),
        ]:
            await pool.execute(
                "INSERT INTO role_configs (role, system_prompt, default_prompt, max_turns) "
                "VALUES ($1, $2, $3, $4)", role, prompt, recommended, 7 if role == "worker" else 0,
            )
        await seed_role_configs(pool)

        rows = {role: await role_configs.get_config(role, pool=pool)
                for role in ("manager", "planner", "worker", "reviewer")}
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
        before = await pool.fetch("SELECT role, system_prompt, default_prompt FROM role_configs ORDER BY 1")
        await seed_role_configs(pool)
        assert await pool.fetch("SELECT role, system_prompt, default_prompt FROM role_configs ORDER BY 1") == before

    run_db(_test)
