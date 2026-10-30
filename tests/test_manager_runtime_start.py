"""A Manager's first turn gives runner initialization its own startup budget."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from looper.agents.agent_settings import AgentSettings
from looper.agents.claude_cli import ResidentTurn
from looper.agents.profile_registry import RuntimeSpec
from looper.agents.runtimes import ADAPTERS, ApiProtocol
from looper.agents.runtimes.dsh_runner import DshRunnerRuntime
from looper.agents.runtimes.pi_runner import PiRunnerRuntime
from looper.agents.tiers import manager as m
from tests.pi_runtime_support import provider
from tests.runtime_model_helpers import resolved_profile
from tests.test_manager_new_session import _web
from tests.test_manager_stop import _manager, _replies, _Session
from tests.test_runner_supervisor import runners as runners  # shared subprocess fixture


@pytest.mark.parametrize("kind", ["pi", "dsh"])
@pytest.mark.parametrize("times_out", [False, True], ids=["cold-start", "timeout"])
def test_runner_first_turn_start_budget(agent_db, runners, tmp_path, monkeypatch, caplog, kind, times_out):
    monkeypatch.setattr(m, "RUNTIME_START_SECONDS", 1, raising=False)
    monkeypatch.setattr(m, "RUNNER_START_SECONDS", 1 if times_out else 5, raising=False)

    async def exercise():
        async with runners(kinds=(kind,), slow_initialize=2) as (owner, processes):
            runtime = (PiRunnerRuntime(owner, pi=None) if kind == "pi"
                       else DshRunnerRuntime(owner, dsh=None))
            monkeypatch.setitem(ADAPTERS, kind, runtime)
            monkeypatch.setattr(runtime, "_check_executable", lambda executable: None)
            account = provider(protocols=frozenset({ApiProtocol.OPENAI_RESPONSES if kind == "pi"
                                                   else ApiProtocol.ANTHROPIC_MESSAGES}))
            profile = resolved_profile("test-profile", "test", RuntimeSpec("test-runtime", kind, kind),
                                       account, "test-model" if kind == "pi" else "deepseek-flash", None)
            session = runtime.resident_session("manager")
            manager, bus, queue = _manager(tmp_path, session)
            manager._claude_runtime = runtime
            monkeypatch.setattr(manager, "_load_agent_settings", AsyncMock(return_value=AgentSettings(profile=profile)))

            async def answer(*args, **kwargs):
                return ResidentTurn({"type": "result", "subtype": "success", "is_error": False,
                                     "result": "ready", "session_id": session._lease.session_id})

            # Only model inference is replaced; spawn, initialize and session/new use the real bridge.
            monkeypatch.setattr(session, "run_turn", answer)
            try:
                await manager._handle(_web("hello"))
                (reply,) = _replies(queue)
                assert len(processes) == 1
                if times_out:
                    cause = f"{runtime.label} did not start within 1 s"
                    opt_out = f'[runtimes] {kind} = "cli" in the ops file; restart Looper'
                    assert cause in reply["error"] and opt_out in reply["error"]
                    assert f"{kind} runner" in reply["error"].lower()
                    assert cause in caplog.text and opt_out in caplog.text
                    (finished,) = agent_db.finish_record.await_args_list
                    assert finished.kwargs["error"] == reply["error"]
                else:
                    assert reply["text"] == "ready" and "error" not in reply
                    requests = [json.loads(line) for line in
                                (session._lease._generation.directory / "requests.jsonl").read_text().splitlines()]
                    assert [request["method"] for request in requests] == ["initialize", "session/new"]
            finally:
                await manager.stop()
                await bus.shutdown()

    asyncio.run(asyncio.wait_for(exercise(), 20))


def test_cli_first_turn_keeps_its_start_budget(agent_db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(m, "RUNTIME_START_SECONDS", 1, raising=False)
    monkeypatch.setattr(m, "RUNNER_START_SECONDS", 5, raising=False)

    async def exercise():
        session = _Session(hold=False, on_start=lambda: asyncio.sleep(2))
        manager, bus, queue = _manager(tmp_path, session)
        try:
            await manager._handle(_web("hello"))
            (reply,) = _replies(queue)
            assert "Claude Code did not start within 1 s" in reply["error"]
            assert "Claude Code did not start within 1 s" in caplog.text
            assert "[runtimes]" not in reply["error"]
            assert not session.started and not session.turns
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(exercise(), 10))


def test_runtime_hosted_session_uses_runner_budget(agent_db, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "RUNTIME_START_SECONDS", 1)
    monkeypatch.setattr(m, "RUNNER_START_SECONDS", 5)
    monkeypatch.setattr(m.CLAUDE_CODE, "hosts_runs", True)

    async def exercise():
        # An adapter may host runs without returning the concrete RunnerSession class.
        session = _Session(hold=False, on_start=lambda: asyncio.sleep(2))
        manager, bus, queue = _manager(tmp_path, session)
        try:
            await manager._handle(_web("hello"))
            (reply,) = _replies(queue)
            assert "error" not in reply and session.turns
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(exercise(), 10))


@pytest.mark.parametrize("exception", [OSError(), RuntimeError()])
def test_empty_start_exception_has_a_cause(agent_db, tmp_path, caplog, exception):
    async def exercise():
        async def fail():
            raise exception

        session = _Session(hold=False, on_start=fail)
        manager, bus, queue = _manager(tmp_path, session)
        try:
            await manager._handle(_web("hello"))
            (reply,) = _replies(queue)
            assert type(exception).__name__ in reply["error"]
            assert type(exception).__name__ in caplog.text
        finally:
            await manager.stop()
            await bus.shutdown()

    asyncio.run(asyncio.wait_for(exercise(), 10))
