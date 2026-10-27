"""Shared loopback API Manager and registry setup for integration tests."""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from manager_mcp_stand_in import manager_endpoint
from looper.agents import profile_registry
from looper.agents.bus import MessageBus
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.runtimes import ADAPTERS
from looper.agents.tiers.manager import ManagerAgent, ManagerConfig
from looper.db import agent_sessions, agent_setting_layers
from tests.fake_model_api import FakeModelAPI, ResponseScript

WEB = channel_address("web")
CUSTOM_PROMPT = "Keep the answer concise."
USAGE = {"input_tokens": 12, "output_tokens": 3,
         "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1}}


async def _profile(pool: Any, base_url: str) -> None:
    await pool.execute(
        "INSERT INTO agent_runtimes (id, name, type, capabilities) VALUES ('rt-api-test', 'Test API', 'api', $1)",
        [cap.value for cap in ADAPTERS["api"].capabilities])
    await pool.execute("INSERT INTO llm_providers (id, name, vendor_type, auth) "
                       "VALUES ('prov-api-test', 'Test API account', 'custom', 'api_key')")
    await pool.execute("INSERT INTO llm_provider_endpoints (provider_id, protocol, base_url) "
                       "VALUES ('prov-api-test', 'openai-responses', $1)", base_url)
    await pool.execute("INSERT INTO llm_provider_secrets (provider_id, api_key) "
                       "VALUES ('prov-api-test', 'test-key')")
    await pool.execute("INSERT INTO llm_models (id, provider_id, name, protocol, input_usd_per_mtok, "
                       "output_usd_per_mtok, cache_read_usd_per_mtok, cache_write_usd_per_mtok, efforts) "
                       "VALUES ('model-api-test', 'prov-api-test', 'test-model', 'openai-responses', "
                       "2, 4, 0.5, 3, '{}')")
    await pool.execute("INSERT INTO agent_profiles (id, name, runtime_id, model_id, custom_prompt) "
                       "VALUES ('agent-api-test', 'Test API profile', 'rt-api-test', 'model-api-test', $1)",
                       CUSTOM_PROMPT)
    await agent_setting_layers.put_layer("role", "manager", None, {"profile_id": "agent-api-test"})


def _message(text: str = "List my tasks.", index: int = 0) -> MessageEnvelope:
    return MessageEnvelope.create(
        source=WEB, target=MANAGER_ADDRESS, message_type=MessageType.USER_MESSAGE,
        payload={"text": text, "channel_context": {"session_id": "api-chat"}},
        correlation_id=f"api-message-{index}", metadata={"origin_channel_id": "channel-web"})


def _replies(bus: MessageBus) -> list[MessageEnvelope]:
    queue = bus.get_queue(WEB.agent_id)
    answers = []
    while not queue.empty():
        envelope = queue.get_nowait()
        if envelope.message_type == MessageType.AGENT_REPLY:
            answers.append(envelope)
    return answers


@asynccontextmanager
async def _manager(pool: Any, root: Path, monkeypatch: pytest.MonkeyPatch,
                   scripts: list[ResponseScript], *, saved: bool = False):
    from looper.agents.runtimes.api.session import FORBIDDEN_ENV

    monkeypatch.setattr(profile_registry, "_login_modules", lambda: {})
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    async with FakeModelAPI(scripts) as model:
        await _profile(pool, model.base_url)
        if saved:
            await agent_sessions.save_session("manager", "api-missing", {}, runtime_type="api",
                                              provider_id="prov-api-test", profile_id="agent-api-test")
        bus = MessageBus(runtime_root=root)
        manager = ManagerAgent(config=ManagerConfig(work_dir=root), runtime_root=root)
        manager._bus = bus
        try:
            async with manager_endpoint(manager, bus, SimpleNamespace(has_planner=lambda _: False), root) as ops:
                yield SimpleNamespace(manager=manager, model=model, bus=bus, ops=ops)
        finally:
            await manager.stop()
            await bus.shutdown()

