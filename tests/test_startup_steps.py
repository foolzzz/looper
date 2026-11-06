"""Startup durations keep their execution order in logs and operator status."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from looper.agents import api_v1, serve
from looper.agents.http_server import AgentHTTPServer
from looper.db import tasks


def test_startup_steps_order_omissions_and_formatting(monkeypatch):
    ticks = iter([101.3, 101.3, 101.41, 101.41, 101.45, 102.6])
    monkeypatch.setattr(serve, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    startup = serve._StartupSteps(100.0)
    with startup.measure("pool"):
        pass
    with startup.measure("llmrec"):
        pass
    assert startup.finish() == "imports=1.3 pool=0.1 llmrec=0.0 total=2.6"
    assert list(startup.steps) == ["imports", "pool", "llmrec", "total"]
    assert startup.steps == pytest.approx({"imports": 1.3, "pool": 0.11, "llmrec": 0.04, "total": 2.6})


def test_startup_steps_records_a_failed_optional_step(monkeypatch):
    ticks = iter([0.0, 1.0, 3.0, 4.0])
    monkeypatch.setattr(serve, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    startup = serve._StartupSteps(0.0)
    with pytest.raises(RuntimeError):
        with startup.measure("channel:telegram"):
            raise RuntimeError("unavailable")
    assert startup.finish() == "imports=0.0 channel:telegram=2.0 total=4.0"


def test_system_status_exposes_completed_startup_steps(monkeypatch):
    async def run():
        startup = serve._StartupSteps(serve.time.monotonic())
        server = AgentHTTPServer(
            host="127.0.0.1", port=0, operator_token="test-token",
            web_adapter=SimpleNamespace(healthcheck=lambda: {
                "channel": "web", "healthy": True, "active_sse_streams": 0}),
            task_router=None, supervisor=SimpleNamespace(agent_status=lambda: {}),
            startup_steps=startup.steps,
        )
        monkeypatch.setattr(tasks, "status_counts", AsyncMock(return_value=[]))
        monkeypatch.setattr(server, "_checks_now", AsyncMock(return_value=[]))
        try:
            with startup.measure("http"):
                await server.start()
            startup.finish()
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.post(f"http://127.0.0.1:{server._port}/api/v1/system/status", json={},
                                             headers={"Authorization": "Bearer test-token"})
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["startup_steps"] == startup.steps
            assert list(result["startup_steps"]) == ["imports", "http", "total"]
            api_v1.validate_arguments(api_v1.find("POST", "/api/v1/system/status").result, result)
        finally:
            await server.stop()

    asyncio.run(run())
