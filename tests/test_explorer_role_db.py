"""Explorer role settings resolve their seeded model without changing other roles."""

from __future__ import annotations

import pytest

from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_explorer_defaults_and_role_overrides(monkeypatch) -> None:
    from looper.agents import agent_settings
    from looper.agents.agent_settings import AgentType, load_agent_settings
    from looper.agents.explorer import EXPLORER_ROLE_PROMPT
    from looper.agents.serve import seed_role_configs
    from looper.db import role_configs

    async def logged_in(*args, **kwargs):
        return None

    monkeypatch.setattr(agent_settings, "_login_problem", logged_in)

    async def run(pool):
        await seed_role_configs(pool)
        config = await role_configs.get_config("explorer", pool=pool)
        assert config["timeout_seconds"] == 300
        assert config["max_turns"] == 40
        assert config["system_prompt"] == EXPLORER_ROLE_PROMPT
        settings = await load_agent_settings(AgentType.EXPLORER, wait=False)
        assert settings.profile.id == "agent-claude-code-sonnet-5-5"
        assert settings.profile.model == "claude-sonnet-5-5"
        assert settings.max_budget_usd == .50
        assert settings.harness.text

        await pool.execute("INSERT INTO agent_setting_layers(scope, role, profile_id, run_budget_usd) "
                           "VALUES ('role', 'explorer', 'agent-claude-code-opus', .25)")
        settings = await load_agent_settings(AgentType.EXPLORER, wait=False)
        assert settings.profile.id == "agent-claude-code-opus"
        assert settings.max_budget_usd == .25
        await role_configs.set_config("explorer", {"system_prompt": "Read carefully", "max_turns": 12}, pool=pool)
        await seed_role_configs(pool)
        config = await role_configs.get_config("explorer", pool=pool)
        assert config["system_prompt"] == "Read carefully"
        assert config["max_turns"] == 12

    run_db(run, monkeypatch)
