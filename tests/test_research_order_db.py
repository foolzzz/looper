"""The research order end to end on a real PostgreSQL, with a stand-in Claude reaching Looper's tools over MCP: off,
the Manager is launched exactly as before; on, its prompt gains the section built from that turn's own tools, so the
user's own turn names the leads search and a group turn does not.

Runs only when LOOPER_TEST_DATABASE_URL points at a disposable database.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper.agents import chat_settings
from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
from looper.agents.ops.service import MANAGER_TOOLS
from tests.db_support import DSN, run_db, run_with_channel_configs
from tests.test_manager_tools_db import _converse

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

HEADING = "## Looking things up"
OP = "ou_operatorsynthetic"


def _prepared(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    from looper.agents.tiers.manager import ManagerAgent

    prepared: list[Any] = []
    prepare = ManagerAgent._prepare_turn

    async def keep(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = await prepare(self, *args, **kwargs)
        if result.signature is not None:
            prepared.append(result)
        return result

    monkeypatch.setattr(ManagerAgent, "_prepare_turn", keep)
    return prepared


def test_off_the_manager_is_launched_as_before_and_on_it_reads_where_to_look(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    async def test(pool: Any) -> None:
        prepared = _prepared(monkeypatch)
        assert chat_settings.research_order() is False
        off = await _converse(tmp_path / "off", [[("looper_overview", {})]], ["hi"])
        monkeypatch.setattr(chat_settings, "_research_order", True)
        on = await _converse(tmp_path / "on", [[("looper_overview", {})]], ["hi"])
        before, after = prepared
        # The setting adds no tool: the listing is the catalog, in its order, either way.
        assert off.tools == on.tools == [tool.name for tool in MANAGER_TOOLS]
        assert HEADING not in before.system_prompt.text
        assert before.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT"
        assert after.system_prompt.layers["contract"]["name"] == "TOOLS_CONTRACT+RESEARCH_ORDER"
        section = after.system_prompt.text[after.system_prompt.text.index(HEADING):]
        assert "looper_tasks, looper_projects" in section and "The web" in section
        assert "leads" not in section.lower()  # leads are off: no leads search to name
        assert after.system_prompt.text.replace("\n" + section, "", 1) == before.system_prompt.text
        # The process signature has main's shape (the prompt's hash changes, nothing is added).
        assert len(after.signature) == len(before.signature)

    run_db(test, monkeypatch, timeout=60)


def test_with_leads_on_the_users_own_turn_names_the_leads_search_and_a_group_turn_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import json

    from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
    from looper.agents.bus import MessageBus
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    prepared = _prepared(monkeypatch)
    monkeypatch.setattr(chat_settings, "_research_order", True)

    def message(index: int, source: Any, context: dict[str, Any], origin: str) -> MessageEnvelope:
        return MessageEnvelope.create(source=source, target=manager_address("manager"),
                                      message_type=MessageType.USER_MESSAGE,
                                      payload={"text": f"hello {index}", "channel_context": context},
                                      correlation_id=f"corr-{index}", metadata={"origin_channel_id": origin})

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO system_settings (key, value) VALUES ('leads.enabled', 'true')")
        await pool.execute("UPDATE channel_configs SET config = $1 WHERE channel_id = 'channel-lark'",
                           json.dumps({"allowed_open_ids": [OP], "operator_sender_id": OP}))
        bus = MessageBus(runtime_root=tmp_path)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[], []]),
                                                    work_dir=tmp_path), runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda task_id: False), tmp_path,
                                    turn_file=tmp_path / "turn.json"):
            await manager._handle(message(0, channel_address("web"), {"session_id": "chat-1"}, "channel-web"))
            await manager._handle(message(1, channel_address("lark", "channel-lark"),
                                          {"chat_id": "oc_group", "chat_type": "group", "sender_id": OP,
                                           "message_id": "om_g1"}, "channel-lark"))
            await manager.stop()
        await bus.shutdown()
        own, group = (turn.system_prompt.text for turn in prepared)
        own_section, group_section = own[own.index(HEADING):], group[group.index(HEADING):]
        assert "looper_leads_search" in own_section
        assert "leads" not in group_section.lower()

    run_with_channel_configs(test, monkeypatch)
