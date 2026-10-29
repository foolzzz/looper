"""Resident session contracts over the real runner bridge and wire codec."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import get_args

import pytest

from looper.agents.runtimes import ApiProtocol, InProcessTools, StopMethod
from looper.agents.runtimes.api.adapter import KEY_ENV
from looper.agents.runtimes.cancel import TurnCancel
from looper.agents.runtimes.api.loop import ToolSpec
from looper.agents.runtimes.runner import protocol as p
from looper.agents.runtimes.runner.session import Descriptor, RunnerSession
from looper.agents.runtimes.runner.bridge import RpcFailure
from runner_pipe_support import connection, send

def run(coro):
    asyncio.run(asyncio.wait_for(coro, 10))

class Tools:
    async def specs(self):
        return [ToolSpec("looper_tasks", "Tasks", {"type": "object"})]

async def limits():
    return {"run_cap": 1, "task_cap": None, "task_spent": 0}

def bind(session):
    session.set_turn_tools(Tools())
    session.set_turn_budget(provider_id="account", model="model", price=lambda m, u: 0.01, limits=limits)

async def opened(tmp_path, bridge, reader, writer, *, changes=None, env=None, refusal=None, check=None, session_options=None):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("unchanged prompt")
    async def release():
        lease.valid = False
        if not bridge._error and lease.session_id in bridge.sessions:
            await bridge.close_session(lease.session_id)
    lease = SimpleNamespace(bridge=bridge, runner_id="fake-1", valid=True, session_id="s",
                            release=release)
    async def open_session(params):
        return await bridge.open_session({**params, "session_key": "private", "home": str(tmp_path), "state_dir": str(tmp_path)})
    lease.open_session = open_session
    async def load_session(params):
        return await bridge.load_session({**params, "session_key": "private", "home": str(tmp_path), "state_dir": str(tmp_path)})
    lease.load_session = load_session
    async def acquire(kind, *, owner=None, state_dir=None):
        options = session_options or {}
        assert kind == "fake" and owner == ("manager" if options.get("owned", True) else None)
        assert state_dir == options.get("state_dir")
        return lease
    session = RunnerSession("manager", SimpleNamespace(acquire=acquire), "fake", **(session_options or {}))
    descriptor = Descriptor("fake", "manager", "account", ApiProtocol.OPENAI_RESPONSES,
        "https://example.invalid", "model", None, 200000, str(prompt), read_only=True)
    descriptor = replace(descriptor, **(changes or {}))
    assert Descriptor.parse(descriptor.to_argv()) == descriptor
    assert "key-sentinel" not in repr(descriptor) + str(descriptor.to_argv())
    starting = asyncio.create_task(session.start(descriptor.to_argv(), cwd=str(tmp_path), signature=1,
                                               env={KEY_ENV: "key-sentinel", **(env or {})}))
    request = await p.read_frame(reader)
    assert request.method == ("session/load" if descriptor.session is not None else "session/new")
    assert "trace_id" not in request.params
    if descriptor.session is not None:
        assert request.params["session_id"] == descriptor.session
    assert request.params["tools"] == {"builtin": list(descriptor.builtin), "platform": [], "read_only": True}
    assert request.params["provider"]["api_key"] == "key-sentinel"
    if check:
        check(request.params)
    if refusal is not None:
        await send(writer, p.Response(request.id, error={"code": int(refusal), "message": "load refused"}))
    else:
        await send(writer, p.Response(request.id, result={"session_id": "s", **({"resumed": True} if descriptor.session else {})}))
    await starting
    assert session.started and session.pid is None and not session.shares_bridge and session.can_interrupt == (refusal is None)
    assert isinstance(session, InProcessTools)
    return session, descriptor

async def begin(session, reader, writer, events):
    bind(session)
    task = asyncio.create_task(session.run_turn("hello", events.append))
    request = await p.read_frame(reader)
    params = request.params
    assert params["tools"] == [{"name": "looper_tasks", "description": "Tasks", "inputSchema": {"type": "object"}}]
    await send(writer, p.Response(request.id, result={**params, "accepted": True}))
    return task, {k: params[k] for k in ("session_id", "trace_id")}

async def update(writer, turn, kind, **values):
    await send(writer, p.Notification("session/update", {**turn, "kind": kind, **values}))

async def charged(reader, writer, turn, index):
    await send(writer, p.Request(f"r-{index}", "budget/authorize", {**turn, "request_id": str(index),
        "provider": "account", "model": "model", "kind": "turn", "estimated_input_tokens": 1, "max_output_tokens": 1}))
    assert (await p.read_frame(reader)).result["allow"]
    await update(writer, turn, "request_end", request_id=str(index), request_kind="turn", model="model",
                 status="ok", input_uncached=1, output=2, cache_read=0, cache_write=0, reasoning=None, complete=True)

async def settle(writer, turn, outcome="completed", requests=1):
    await update(writer, turn, "settled", outcome=outcome, final_text="answer", requests=requests, usage_complete=True)

async def close(session, reader, writer, **kwargs):
    task = asyncio.create_task(session.stop(**kwargs))
    request = await p.read_frame(reader)
    assert request.method == "session/close" and "trace_id" not in request.params
    await send(writer, p.Response(request.id, result={}))
    await task

def test_multiturn_charge_trace_and_cleared_context(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            session, _ = await opened(tmp_path, bridge, reader, writer)
            events, traces = [], []
            for index in (1, 2):
                task, turn = await begin(session, reader, writer, events)
                traces.append(turn["trace_id"])
                await charged(reader, writer, turn, index)
                await settle(writer, turn)
                result = await task
                assert result.usage.cost_usd == 0.01 and result.usage.input_tokens == 1
                assert result.prompt_sent and result.session_started and result.usage_complete
                assert result.result_event["modelUsage"]["model"]["inputTokens"] == index
                assert session.totals.cost_usd == index * 0.01
                assert session._tools is session._budget is None
            assert len(set(traces)) == 2 and all(e["trace_id"] in traces for e in events)
            # The recorder takes a run's model from its init; a resident session's every turn is a record.
            inits = [e for e in events if e["type"] == "system" and e.get("subtype") == "init"]
            assert [(e["trace_id"], e.get("model")) for e in inits] == [(trace, "model") for trace in traces]
            assert all(e["session_started"] is True for e in inits)
            failed = await session.run_turn("missing context", events.append)
            assert not failed.prompt_sent and "budget context" in failed.result_event["errors"][0]
            cancel = TurnCancel()
            cancel.request()
            bind(session)
            cancelled = await session.run_turn("already stopped", events.append, cancel=cancel)
            assert cancelled.stopped == StopMethod.CANCEL and cancelled.result_event["trace_id"] not in traces
            await close(session, reader, writer, kill=True)
            assert not bridge._error and await session.interrupt() is None
    run(exercise())


@pytest.mark.parametrize("failure", ["timeout", "refused"])
def test_failed_prompt_records_model_and_current_session(tmp_path, monkeypatch, failure):
    from looper.agents.claude_cli import ClaudeRunRecorder
    from tests.db_support import run_db

    async def exercise(pool):
        async with connection(control_timeout=0.1) as (bridge, reader, writer, _):
            session, descriptor = await opened(tmp_path, bridge, reader, writer)
            recorder = ClaudeRunRecorder(agent_id="manager", task_id=None, command=descriptor.to_argv(),
                cwd=str(tmp_path), input_text="hello", session_id="s")
            await recorder.start()
            bind(session)
            task = asyncio.create_task(session.run_turn("hello", recorder.record))
            request = await p.read_frame(reader)
            if failure == "refused":
                await send(writer, p.Response(request.id, error={"code": int(p.ErrorCode.INVALID_PARAMS),
                    "message": "prompt refused", "data": {k: request.params[k] for k in ("session_id", "trace_id")}}))
            result = await task
            # A hosted channel emits a returned failure only when the bridge did not emit it.
            if failure == "refused":
                await recorder.record(result.result_event)
            await recorder.finish(output_text=None, result_event=result.result_event)
            row = await pool.fetchrow("SELECT * FROM llm_records WHERE id = $1", recorder.record_id)
            assert row["status"] == "failed"
            assert (row["model"], row["session_id"]) == (descriptor.model, "s")
            from looper.db.llm_records import list_events
            events = [row["event"] for row in await list_events(recorder.record_id)]
            assert [event["type"] for event in events] == ["system", "result"]
            assert events[0]["session_id"] == "s" and events[0]["model"] == descriptor.model
            assert events[0]["trace_id"] == result.result_event["trace_id"]
            assert events[0]["session_started"] is False
            assert not result.session_started and not result.usage_complete
            if failure == "refused":
                await close(session, reader, writer)
            else:
                await session.stop()

    run_db(exercise, monkeypatch, timeout=20)


@pytest.mark.parametrize("role", ["manager", "planner"])
def test_rejected_first_prompt_keeps_handoff_until_accepted(tmp_path, monkeypatch, agent_db, role):
    from unittest.mock import AsyncMock
    from looper.agents.bus import MessageBus
    from looper.agents.tiers import manager as manager_module, planner as planner_module
    from tests.db_support import until
    from tests.test_manager_new_session import _web
    from tests.runtime_model_helpers import builtin_launch

    async def exercise():
        launch = builtin_launch()
        async with connection() as (bridge, reader, writer, _):
            session, descriptor = await opened(tmp_path, bridge, reader, writer, changes={
                "role": role, "model": launch.model, "provider_id": launch.profile.provider.id})
            monkeypatch.setattr(session, "stale", lambda signature: False)
            bus = MessageBus(runtime_root=tmp_path)
            if role == "manager":
                agent = manager_module.ManagerAgent(
                    config=manager_module.ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path)
                agent._session_loaded = True
                agent._session_runtime = launch.runtime.type
                agent._bus = bus
                async def exchange(first):
                    return await agent._run_turn("Continue", _web("Continue"))
                def session_id():
                    return agent._session_id
            else:
                agent = planner_module.PlannerAgent(task_id="handoff-task", runtime_root=tmp_path)
                agent._plan = planner_module.TaskPlan("plan", "goal", "Continue", str(tmp_path), steps=[
                    planner_module.PlanStep(step_id="one", index=0, instruction="Continue")],
                    session_runtime=launch.runtime.type, session_profile=launch.profile.id,
                    session_provider=launch.profile.provider.id)
                agent._process = planner_module._Process(descriptor.to_argv(), None,
                                                        planner_module.SystemPrompt("Continue"), launch)
                monkeypatch.setattr(agent, "_claude_command", lambda *args: descriptor.to_argv())
                monkeypatch.setattr(planner_module, "load_agent_settings", AsyncMock(return_value=launch.settings))
                monkeypatch.setattr(planner_module.db_llm_records, "task_costs",
                                    AsyncMock(return_value={"handoff-task": {"cost_usd": 0}}))
                async def exchange(first):
                    if first:
                        return await agent._exchange(agent._resume_brief(agent._plan) + "\n\nContinue",
                                                     agent._process, 2)
                    return await agent._run_turn("Continue")
                def session_id():
                    return agent._plan.session_id
            agent._claude = session
            agent._claude_runtime = launch.runtime
            session.label = agent.agent_id
            agent._pending_switch = switch = manager_module.session_switch.session_lost("previous session lost")
            agent._pending_handoff = handoff = "Continue checking the uncommitted parser change."
            # Keep tier preparation real; supply external settings, costs and platform tools.
            monkeypatch.setattr(agent, "_load_agent_settings", AsyncMock(return_value=launch.settings))
            try:
                for accepted in (False, True, True):
                    session.set_turn_tools(Tools())
                    task = asyncio.create_task(exchange(first=not accepted))
                    request = await p.read_frame(reader)
                    assert request.method == "session/prompt"
                    assert (handoff in request.params["text"]) is (agent._pending_handoff is not None)
                    turn = {k: request.params[k] for k in ("session_id", "trace_id")}
                    if not accepted:
                        assert handoff in request.params["text"]
                        await send(writer, p.Response(request.id, error={"code": int(p.ErrorCode.INVALID_PARAMS),
                            "message": "prompt refused", "data": turn}))
                        with pytest.raises(RuntimeError if role == "manager" else planner_module.PlannerTurnError):
                            await task
                        assert session_id() is None
                        assert agent._pending_handoff == handoff and agent._pending_switch == switch
                        assert agent_db.save_session.await_args_list
                        assert all(call.args[1] is None for call in agent_db.save_session.await_args_list)
                    else:
                        await send(writer, p.Response(request.id, result={**turn, "accepted": True}))
                        # Acceptance owns the brief even if the model has not answered yet.
                        await until(lambda: session_id() == "s")
                        assert agent._pending_handoff is None and agent._pending_switch is None
                        await settle(writer, turn, requests=0)
                        assert await task == "answer"
                        assert agent_db.store.sessions[agent.agent_id]["session_id"] == "s"
            finally:
                if bridge._error:
                    await session.stop()
                else:
                    await close(session, reader, writer)
                await bus.shutdown()
    run(exercise())

def test_cancel_and_steer_preserve_trace_and_close_admission(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            session, _ = await opened(tmp_path, bridge, reader, writer)
            events = []
            task, turn = await begin(session, reader, writer, events)
            steering = asyncio.create_task(session.steer("follow up", message_id="stable"))
            request = await p.read_frame(reader)
            assert request.params == {**turn, "message_id": "stable", "text": "follow up", "if_idle": "reject"}
            await send(writer, p.Response(request.id, result={**turn, "status": "queued"}))
            assert (await steering)["status"] == "queued"
            await update(writer, turn, "steer_applied", message_id="stable", after_request_id="q",
                         after_tool_batch=True, new_turn=False)
            stopping = asyncio.create_task(session.interrupt())
            request = await p.read_frame(reader)
            assert request.method == "session/cancel" and not session._turn._active
            await send(writer, p.Request("r-8", "budget/authorize", {**turn, "request_id": "late", "provider": "account",
                "model": "model", "kind": "turn", "estimated_input_tokens": 1, "max_output_tokens": 1}))
            assert not (await p.read_frame(reader)).result["allow"]
            await send(writer, p.Response(request.id, result=turn))
            await settle(writer, turn, "cancelled", requests=0)
            assert await stopping == StopMethod.CANCEL
            assert (await task).stopped == StopMethod.CANCEL
            assert len([e for e in events if e["type"] == "result"]) == 1
            assert any(e.get("subtype") == "steer_applied" for e in events)
            racing, race_turn = await begin(session, reader, writer, events)
            steering = asyncio.create_task(session.steer("racing", message_id="race"))
            request = await p.read_frame(reader)
            await update(writer, race_turn, "steer_dropped", message_id="race", reason="turn_ended")
            await settle(writer, race_turn, requests=0)
            await send(writer, p.Response(request.id, result={**race_turn, "status": "queued"}))
            assert (await steering)["status"] == "queued" and (await racing).session_started
            assert any(e.get("subtype") == "steer_dropped" for e in events)
            with pytest.raises(p.ProtocolError) as error:
                await session.steer("idle", message_id="idle")
            assert error.value.code == p.ErrorCode.NOT_RUNNING
            await close(session, reader, writer)
    run(exercise())

@pytest.mark.parametrize("before_prompt", [False, True])
def test_loss_never_replays(tmp_path, before_prompt):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            session, descriptor = await opened(tmp_path, bridge, reader, writer)
            if before_prompt:
                async def specs():
                    writer.close()
                    await bridge._reader_task
                    return []
                bind(session)
                session.set_turn_tools(SimpleNamespace(specs=specs))
                result = await session.run_turn("never sent", [].append)
                assert "lost before prompting" in result.result_event["errors"][0]
            else:
                task, _ = await begin(session, reader, writer, [])
                writer.close()
                result = await task
                assert result.result_event["runner"]["outcome"] == "lost"
            assert not result.usage_complete and not session.started and result.prompt_sent != before_prompt
    run(exercise())



def test_resume_loads_the_saved_session(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            session, _ = await opened(tmp_path, bridge, reader, writer, changes={"session": "s"})
            events = []
            task, turn = await begin(session, reader, writer, events)
            await charged(reader, writer, turn, 1)
            await settle(writer, turn)
            result = await task
            assert result.session_started and result.prompt_sent and result.usage.cost_usd == 0.01
            assert all(e["session_id"] == "s" for e in events)
            await close(session, reader, writer)
            assert not session.started
    run(exercise())


@pytest.mark.parametrize("code", [p.ErrorCode.SESSION_NOT_FOUND, p.ErrorCode.SESSION_OWNED, p.ErrorCode.INVALID_PARAMS])
def test_resume_not_found_takes_the_resume_failure_path(tmp_path, code):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            opening = opened(tmp_path, bridge, reader, writer, changes={"session": "s"}, refusal=code)
            if code == p.ErrorCode.INVALID_PARAMS:
                with pytest.raises(RpcFailure) as error:
                    await opening
                assert error.value.code == code
            else:
                session, _ = await opening
                events = []
                result = await session.run_turn("resume", events.append)
                assert not result.session_started and not result.prompt_sent
                assert "could not be resumed" in result.result_event["errors"][0]
                assert events == [{"type": "system", "subtype": "init", "session_id": "s",
                    "model": "model", "trace_id": result.result_event["trace_id"], "session_started": False}]
                assert session._lease is None
                await session.stop()
                assert not session.started
            assert not bridge.sessions and not bridge._error
    run(exercise())


def test_tool_environment_and_account_id(tmp_path, caplog):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            env = {"LANG": "C", "TOOL_SETTING": "value"}
            def check(params):
                assert params["env"] == env
                assert params["provider"] == {"account_id": "account", "reasoning": True,
                    "protocol": "openai-responses", "base_url": "https://example.invalid", "model": "model",
                    "effort": None, "context_window": 200000, "api_key": "key-sentinel"}
                assert params["system_prompt"] == {"mode": "append", "text": "unchanged prompt"}
                assert params["tools"]["builtin"] == ["read", "bash"]
            session, descriptor = await opened(tmp_path, bridge, reader, writer,
                changes={"system_prompt_mode": "append", "reasoning": True, "builtin": ("read", "bash")},
                env=env, check=check)
            assert "key-sentinel" not in repr(descriptor) + str(descriptor.to_argv()) + caplog.text
            assert env == {"LANG": "C", "TOOL_SETTING": "value"}
            await close(session, reader, writer)
    run(exercise())


@pytest.mark.parametrize(("flag", "value"), [("--system-prompt-mode", "invalid"), ("--reasoning", "true"),
    ("--tool-timeout-s", "0"), ("--builtin", ""), ("--builtin", "Read"), ("--builtin", "read-file"), ("--builtin", "read1")])
def test_descriptor_rejects_invalid_options(flag, value):
    descriptor = Descriptor("fake", "manager", "account", ApiProtocol.OPENAI_RESPONSES,
        "https://example.invalid", "model", None, None, "prompt.md", builtin=("read",))
    argv = descriptor.to_argv()
    argv[argv.index(flag) + 1] = value
    with pytest.raises(ValueError):
        Descriptor.parse(argv)



def test_run_session_opens_without_owner_state(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            def check(params):
                assert params["role"] == "worker"
                assert params["limits"] == {"max_requests_per_turn": 7,
                    "tool_timeout_s": 3600, "tool_output_bytes": 102400}
            session, _ = await opened(tmp_path, bridge, reader, writer,
                changes={"role": "worker", "max_requests": 7, "tool_timeout_s": 3600}, check=check,
                session_options={"owned": False, "state_dir": tmp_path / "run"})
            await close(session, reader, writer)
    run(exercise())


def fork_handshake():
    return {"capabilities": {"fork": True, "cancel": True, "usage": {"per_request": True},
        "authorize_covers": list(get_args(p.RequestKind))}}


def test_fork_opens_a_new_session_from_the_saved_id(tmp_path):
    async def exercise():
        async with connection(fork_handshake()) as (bridge, reader, writer, _):
            def check(params):
                assert params["fork_from"] == {"session_id": "source"}
                assert "session_id" not in params
                assert params["tools"]["read_only"]
            session, _ = await opened(tmp_path, bridge, reader, writer,
                changes={"role": "reviewer", "fork_from": "source"}, check=check)
            task, turn = await begin(session, reader, writer, [])
            assert turn["session_id"] == "s"
            await charged(reader, writer, turn, 1)
            await settle(writer, turn)
            result = await task
            assert result.session_started and result.prompt_sent and result.usage.cost_usd == 0.01
            await close(session, reader, writer)
    run(exercise())


@pytest.mark.parametrize("code", [p.ErrorCode.SESSION_NOT_FOUND, p.ErrorCode.SESSION_OWNED,
    p.ErrorCode.INVALID_PARAMS])
def test_fork_refusal_takes_the_resume_failure_path(tmp_path, code):
    async def exercise():
        async with connection(fork_handshake()) as (bridge, reader, writer, _):
            opening = opened(tmp_path, bridge, reader, writer,
                changes={"role": "worker", "fork_from": "source"}, refusal=code)
            if code == p.ErrorCode.INVALID_PARAMS:
                with pytest.raises(RpcFailure) as error:
                    await opening
                assert error.value.code == code
            else:
                session, _ = await opening
                result = await session.run_turn("consult", [].append)
                assert not result.session_started and not result.prompt_sent
                assert result.result_event["errors"] == [
                    "runner saved session is unavailable; could not be resumed"]
                assert session._lease is None
                await session.stop()
            assert not bridge.sessions and not reader._buffer
    run(exercise())


def test_fork_without_capability_is_unsupported(tmp_path):
    async def exercise():
        async with connection() as (bridge, reader, writer, _):
            released = []
            async def release():
                released.append(True)
            async def acquire(kind, *, owner=None, state_dir=None):
                return SimpleNamespace(bridge=bridge, release=release)
            prompt = tmp_path / "prompt.md"
            prompt.write_text("consult")
            descriptor = Descriptor("fake", "worker", "account", ApiProtocol.OPENAI_RESPONSES,
                "https://example.invalid", "model", None, None, str(prompt), fork_from="source")
            session = RunnerSession("w", SimpleNamespace(acquire=acquire), "fake", owned=False)
            with pytest.raises(p.ProtocolError) as error:
                await session.start(descriptor.to_argv(), cwd=str(tmp_path), signature=1, env={KEY_ENV: "key"})
            assert error.value.code == p.ErrorCode.UNSUPPORTED
            assert released == [True] and not reader._buffer and not bridge.sessions
    run(exercise())


def test_descriptor_fork_round_trip_and_exclusive_saved_session():
    descriptor = Descriptor("fake", "reviewer", "account", ApiProtocol.OPENAI_RESPONSES,
        "https://example.invalid", "model", None, None, "prompt.md", tool_timeout_s=3600, fork_from="source")
    argv = descriptor.to_argv()
    assert Descriptor.parse(argv) == descriptor
    assert argv[-2:] == ["--fork-from", "source"]
    with pytest.raises(ValueError):
        Descriptor.parse(argv + ["--session", "saved"])
    with pytest.raises(ValueError):
        replace(descriptor, session="saved")
