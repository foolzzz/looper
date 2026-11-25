"""Default-off Manager records retain the pre-metrics metadata contract."""
import json
from types import SimpleNamespace

import pytest

from manager_mcp_stand_in import manager_endpoint, mcp_manager_claude
from tests.db_support import DSN, run_db

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_default_off_preserves_model_metadata_and_tags_only_proactive_bus_sends(monkeypatch, tmp_path):
    from looper.agents import chat_settings
    from looper.agents.bus import MessageBus
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    async def test(pool):
        monkeypatch.setattr(chat_settings, "_metrics_enabled", False)
        manager = ManagerAgent(config=ManagerConfig(claude_bin=mcp_manager_claude(tmp_path, [[]]), work_dir=tmp_path), runtime_root=tmp_path)
        bus = MessageBus(runtime_root=tmp_path)
        manager._bus = bus
        async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), tmp_path):
            message = MessageEnvelope.create(source=channel_address("web"), target=manager_address("manager"),
                message_type=MessageType.USER_MESSAGE, payload={"text":"hi", "channel_context":{"session_id":"chat-off"}}, correlation_id="off-turn")
            await manager._handle(message)
            await manager._send_own_reply(message, "autonomous")
            await manager.stop()
        metadata = json.loads(await pool.fetchval("SELECT metadata::text FROM llm_records WHERE agent_id = 'manager' ORDER BY id DESC LIMIT 1"))
        assert "turn_metrics" not in metadata
        assert set(metadata) == {'command', 'cwd', 'system_prompt', 'system_prompt_sha256', 'prompt_layers', 'profile', 'model_choice', 'settings_sources', 'system_prompt_mode', 'llm_model', 'pricing', 'project_instructions', 'library'}, sorted(metadata)
        assert await pool.fetchval("SELECT count(*) FROM proactive_deliveries") == 0
        queue = bus.get_queue(channel_address("web").agent_id)
        proactive = []
        while not queue.empty():
            envelope = queue.get_nowait()
            if envelope.payload.get("autonomous"):
                proactive.append(envelope.metadata["proactive"])
                assert envelope.payload["text"] == "autonomous"
            else:
                assert "proactive" not in envelope.metadata
        assert proactive == [{"source": "autonomous", "source_ref": None, "manager_id": "manager"}]
        await bus.shutdown()
    run_db(test, monkeypatch, timeout=90)
