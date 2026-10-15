"""The effort's upgrade on a real PostgreSQL: a database whose layers set an effort keeps what every run gets.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database, in a
new schema of its own, dropped afterwards. The database before the upgrade is
this schema with the column it drops (``agent_setting_layers.effort``) and
without the upgrade's ``schema_migrations`` row; every role at every place,
and a task's choice, then resolves under the new rule to what the old rule
(``test_layer_effort._old``) gave.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from tests.test_catalog_db import DSN, _fresh_schema
from tests.test_layer_effort import _old

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

OPUS = "agent-claude-code-opus"
# The maintainer's install (the built-in profile with xhigh, high for the Workers), and a project that runs sonnet
# with an effort of its own for its Reviewers.
_BEFORE = """
ALTER TABLE agent_setting_layers ADD COLUMN effort TEXT;
DELETE FROM schema_migrations WHERE name = 'layer_effort_on_profiles';
INSERT INTO agent_profiles (id, name, runtime_id, provider_id, model, agent_teams) VALUES
    ('agent-sonnet', 'claude-code/sonnet', 'rt-claude-code', 'prov-claude-subscription', 'sonnet', true),
    ('agent-haiku', 'claude-code/haiku', 'rt-claude-code', 'prov-claude-subscription', 'haiku', true);
INSERT INTO projects (id, name) VALUES ('proj-x', 'calc');
INSERT INTO tasks (id, requirement, project_id) VALUES ('task-x', 'Add add().', 'proj-x');
INSERT INTO task_profile_overrides (task_id, role, profile_id) VALUES ('task-x', 'worker', 'agent-haiku');
INSERT INTO agent_setting_layers (scope, role, project_id, profile_id, effort) VALUES
    ('system', NULL, NULL, 'agent-claude-code-opus', 'xhigh'),
    ('role', 'worker', NULL, NULL, 'high'),
    ('project', NULL, 'proj-x', 'agent-sonnet', NULL),
    ('role_project', 'reviewer', 'proj-x', NULL, 'low');
"""


def test_the_upgrade_moves_each_effort_onto_profiles_and_every_run_keeps_its_profile_and_effort(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from looper.agents.agent_settings import TASK_ROLES, AgentType, Layer, index_layers, resolve
    from looper.agents.layer_effort import Before
    from looper.agents.profile_registry import AgentProfile
    from looper.db import agent_setting_layers, connection
    from looper.db.profile_registry import get_profile

    async def _selected(pool: Any) -> dict[tuple[Any, ...], str | None]:
        return {(r["scope"], r["role"], r["project_id"]): r["name"] for r in await pool.fetch(
            "SELECT l.scope, l.role, l.project_id, a.name FROM agent_setting_layers l "
            "LEFT JOIN agent_profiles a ON a.id = l.profile_id")}

    async def _main() -> None:
        async with _fresh_schema() as pool:
            await connection.ensure_schema(pool)
            await pool.execute(_BEFORE)
            profiles = {pid: AgentProfile.from_row(await get_profile(pid, pool=pool))
                        for pid in (OPUS, "agent-sonnet", "agent-haiku")}
            before = {(Layer.SYSTEM, None, None): Before(profiles[OPUS], "xhigh"),
                      (Layer.ROLE, AgentType.WORKER, None): Before(effort="high"),
                      (Layer.PROJECT, None, "proj-x"): Before(profiles["agent-sonnet"]),
                      (Layer.ROLE_PROJECT, AgentType.REVIEWER, "proj-x"): Before(effort="low")}

            with caplog.at_level(logging.INFO, logger="looper.agents.layer_effort"):
                await connection.ensure_schema(pool)

            assert await _selected(pool) == {
                ("system", None, None): "claude-code/opus · xhigh",
                ("role", "worker", None): "claude-code/opus · high",
                # Most of the project's roles ran sonnet with xhigh; its Workers ran it with high, its Reviewers low.
                ("project", None, "proj-x"): "claude-code/sonnet · xhigh",
                ("role_project", "worker", "proj-x"): "claude-code/sonnet · high",
                ("role_project", "reviewer", "proj-x"): "claude-code/sonnet · low",
            }
            # The task's Workers ran haiku with the Workers' high.
            assert await pool.fetchval("SELECT a.name FROM task_profile_overrides o JOIN agent_profiles a "
                                       "ON a.id = o.profile_id") == "claude-code/haiku · high"
            for role, project in [(None, None), *((role, None) for role in AgentType),
                                  *((role, "proj-x") for role in TASK_ROLES)]:
                settings = resolve(role, index_layers(await agent_setting_layers.list_layers(project, pool=pool),
                                                      project), project)
                profile = settings.profile
                assert (profile.runtime.id, profile.provider.id, profile.model, profile.agent_teams,
                        settings.effort or None) == _old(before, role, project), (role, project)
            # One line per profile added and per layer and task changed.
            lines = [r.getMessage() for r in caplog.records if r.name == "looper.agents.layer_effort"]
            assert sum("added Agent Profile" in line for line in lines) == 6
            assert sum(" selects " in line for line in lines) == 5
            assert sum("the worker of task task-x runs 'claude-code/haiku · high'" in line for line in lines) == 1
            assert not await pool.fetchval(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'agent_setting_layers' AND column_name = 'effort'")

            # Once: applying the schema again changes nothing.
            layers, count = await _selected(pool), await pool.fetchval("SELECT count(*) FROM agent_profiles")
            await connection.ensure_schema(pool)
            assert (await _selected(pool), await pool.fetchval("SELECT count(*) FROM agent_profiles")) == (
                layers, count)

    asyncio.run(_main())
