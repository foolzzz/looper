"""Absent settings leave lead access off and add nothing to agents' tools or composed prompts."""

import asyncio

import pytest

from looper import cli
from looper.agents import agent_settings, api_v1, projects
from looper.agents.ops import clients, planner, room, service
from looper.agents.system_prompt import build_system_prompt
from looper.agents.tiers import manager, planner as planner_tier, worker
from tests.db_support import DSN, fresh_schema, v1_api


def test_default_agents_have_no_lead_tools_or_prompt_additions(monkeypatch, tmp_path):
    tools = (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS, *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)
    texts = [f"{tool.name} {tool.description} {tool.schema}" for tool in tools]
    texts += [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT, worker.DEFAULT_SYSTEM_PROMPT,
              agent_settings.DEFAULT_HARNESS_PROMPT]
    assert not any("looper_leads" in text or "message leads" in text.lower() for text in texts)
    monkeypatch.setattr(projects, "projects_root", lambda: "")
    instance = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
    settings = agent_settings.resolve("manager", {})
    expected = build_system_prompt(harness=settings.harness, custom_prompt=settings.profile.custom_prompt,
                                  role_id="manager", role=manager.DEFAULT_SYSTEM_PROMPT, recommended=True,
                                  contract_name="TOOLS_CONTRACT", contract=manager.TOOLS_CONTRACT)
    assert instance._composed_prompt(settings).text.encode() == expected.text.encode()
    assert all(op.tool is None and op.operator_only for op in api_v1.operations()
               if op.operation_id.startswith("leads."))


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
def test_no_setting_means_cli_disabled_and_api_conflict(monkeypatch, tmp_path, capsys):
    from urllib.parse import quote
    from looper.db import connection

    monkeypatch.delenv("LOOPER_AGENT_RUN", raising=False)

    async def check():
        async with fresh_schema() as pool:
            await connection.ensure_schema(pool)
            schema = await pool.fetchval("SELECT current_schema()")
            url = DSN + ("&" if "?" in DSN else "?") + "search_path=" + quote(schema)
            config = tmp_path / "server.toml"
            config.write_text(f'[database]\nurl = "{url}"\n')
            config.chmod(0o600)
            args = ["leads", "search", "--config", str(config), "--runtime-root", str(tmp_path / "runtime")]
            assert await asyncio.to_thread(cli.main, args) == 3
            assert "消息线索没有启用" in capsys.readouterr().err
            monkeypatch.setattr(connection, "_pool", pool)
            async with v1_api(tmp_path / "api", pool) as call:
                status, answer = await call("leads.search")
                assert status == 409 and answer["error"]["code"] == "conflict"

    asyncio.run(check())
