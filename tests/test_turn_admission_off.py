"""Per-item recorded turn contracts with account admission disabled."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


from looper.agents import admission, claude_cli, lanes, lanes_settings
from looper.agents.agent_settings import AgentType
from looper.agents.message import MANAGER_ADDRESS, MessageEnvelope, MessageType, channel_address
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.run_usage import Usage
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import manager, planner
from looper.agents.tiers.planner_protocol import TurnKind
from looper.agents.tiers.turn_token import TurnPhase, TurnToken
from looper.db import llm_records, system_settings
from tests.runtime_model_helpers import builtin_launch

FIXTURE = Path(__file__).parent / "fixtures" / "turn_admission_off.json"
BASE_SHA = "0d9e121c1eb124282c33b5e8352164f30d212425"


def _envelope(kind=MessageType.USER_MESSAGE):
    value = MessageEnvelope.create(source=channel_address("web"), target=MANAGER_ADDRESS,
        message_type=kind, payload={"text": "Inspect the synthetic task", "channel_context": {"session_id": "sample"}},
        metadata={"origin_channel_id": "channel-web"})
    return replace(value, message_id="synthetic-message", correlation_id="synthetic-correlation")


def _token(origin):
    token = TurnToken("synthetic-message", None, "web", "sample", datetime(2026, 1, 1, tzinfo=timezone.utc))
    token.phase = TurnPhase.RUNNING
    token.ops_turn = Turn(channel="web", chat="sample", origin=origin)
    return token


def capture(monkeypatch):
    """Drive real exchanges/recorders with synthetic resident sessions; no model or database."""
    monkeypatch.setattr(admission._controller, "active", False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(claude_cli, "_clock", lambda: 100.0)
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))
    start = AsyncMock(return_value=12)
    monkeypatch.setattr(llm_records, "start_record", start)
    for name in ("append_events", "finish_record", "touch_record"):
        monkeypatch.setattr(llm_records, name, AsyncMock())
    monkeypatch.setattr(claude_cli.ClaudeRunRecorder, "_after_run", AsyncMock())
    output = {}

    def record_item(prompt, text, command, sent=()):
        return {"system_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "turn_text": text, "command": command,
                "metadata_keys": sorted(start.await_args.kwargs["metadata"]), "envelopes": list(sent)}

    async def run():
        launch = builtin_launch(role=AgentType.PLANNER)
        agent = planner.PlannerAgent(task_id="synthetic-task", runtime_root=Path("/runtime"))
        agent._plan = planner.TaskPlan("synthetic-plan", "synthetic-goal", "Inspect", "/workspace")
        command = agent._claude_command(Path("/runtime/planner.md"),
            planner.PlannerSettings(planner.DEFAULT_SYSTEM_PROMPT, 30, 5, 0), launch, "/workspace")
        prompt = SystemPrompt(planner.DEFAULT_SYSTEM_PROMPT)
        process = planner._Process(command, None, prompt, launch)

        async def turn(text, on_event, **kwargs):
            if text == "/compact":
                await on_event({"type": "system", "subtype": "compact_boundary",
                                "compact_metadata": {"trigger": "manual", "post_tokens": 10}})
            event = {"type": "result", "subtype": "success", "result": "synthetic reply", "total_cost_usd": 0}
            await on_event(event)
            return claude_cli.ResidentTurn(event)

        agent._claude = SimpleNamespace(started=False, pid=None, totals=Usage(), run_turn=turn)
        monkeypatch.setattr(agent, "_read_run_deadline", AsyncMock())
        monkeypatch.setattr(agent, "_read_roles", AsyncMock())
        monkeypatch.setattr(agent, "_save_session", AsyncMock())
        monkeypatch.setattr(agent, "_report_read_only", AsyncMock())
        for name, kind, text in (("planner_plan", TurnKind.PLAN, "[PLAN] Inspect the synthetic task"),
                                 ("planner_chat", TurnKind.CHAT, "[CHAT] Explain the result"),
                                 ("planner_compact", None, "/compact")):
            async def exchange(message):
                return await agent._exchange(message, process, 30, maintenance=kind is None)
            if kind is None:
                assert await exchange(text) == "synthetic reply"
            else:
                assert await agent._ask(kind, text, lambda value: value, run=exchange) == "synthetic reply"
            output[name] = record_item(prompt.text, start.await_args.kwargs["input_text"], command)

        agent = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=Path("/workspace")),
                                     runtime_root=Path("/runtime"), clock=lambda: 100.0)
        sent = []
        agent._bus = SimpleNamespace(publish=AsyncMock(side_effect=lambda envelope: sent.append({
            "type": envelope.message_type.value, "source": str(envelope.source), "target": str(envelope.target),
            "payload": envelope.payload, "metadata": envelope.metadata,
            "reply_to": envelope.reply_to, "correlation_id": envelope.correlation_id})))
        monkeypatch.setattr(agent._router, "release", AsyncMock())
        launch = builtin_launch(role=AgentType.MANAGER)
        prompt = SystemPrompt(manager.DEFAULT_SYSTEM_PROMPT)
        command = agent._claude_command(Path("/runtime/manager.md"), launch)

        async def start_turn(token, prepared):
            prepared.run.recorder = claude_cli.ClaudeRunRecorder(agent_id=agent.agent_id, task_id=None,
                command=command, cwd="/workspace", input_text=prepared.message,
                system_prompt=prompt, launch=launch)
            await prepared.run.recorder.start()

        async def converse(token, prepared):
            await agent._send_status(prepared.envelope, "Thinking", phase=True, activity="thinking")
            await prepared.run.recorder.finish(output_text="synthetic reply", result_event={
                "type": "result", "subtype": "success", "result": "synthetic reply", "total_cost_usd": 0})
            await agent._bus.publish(MessageEnvelope.create(source=agent.agent_address,
                target=prepared.envelope.source, message_type=MessageType.AGENT_REPLY,
                payload={"text": "synthetic reply"}, reply_to=prepared.envelope.message_id,
                correlation_id=prepared.envelope.correlation_id))
            return "synthetic reply"

        monkeypatch.setattr(agent, "_start_turn", start_turn)
        monkeypatch.setattr(agent, "_converse", converse)
        for name, origin, text in (("manager_user", TurnOrigin.USER, "Inspect the synthetic task"),
                                   ("manager_completion", TurnOrigin.COMPLETION,
                                    "[COMPLETION] Looper reports tasks that ended.")):
            sent.clear()
            token = _token(origin)
            prepared = manager.Prepared(envelope=_envelope(), turn=token.ops_turn, launch=launch,
                message=text, system_prompt=prompt, session=agent._sessions["main"])
            assert await agent._run_prepared(token, prepared) == "synthetic reply"
            output[name] = record_item(prompt.text, start.await_args.kwargs["input_text"], command, sent)

        table = []
        for origin, interrupt, merge in itertools.product((None, *TurnOrigin), (False, True), (False, True)):
            token = _token(origin) if origin is not None else None
            agent = SimpleNamespace(ops=Mock(), _mid_turn=SimpleNamespace(merge=AsyncMock(return_value=merge)),
                                    _router=SimpleNamespace(void_idle_checks=AsyncMock()))
            session = SimpleNamespace(running_token=token, claude=SimpleNamespace(can_interrupt=interrupt))
            result = await lanes.route_arrival(agent, session, _envelope())
            table.append({"origin": origin.value if origin else None, "interrupt": interrupt, "merge": merge,
                          "merged": result, "stopped": bool(token and token.cancel.requested),
                          "cleanup": token.cancel.cleanup.value if token else None,
                          "preempted_by": token.preempted_by if token else None})
        output["route_arrival"] = table

    asyncio.run(run())
    return output


def test_disabled_turns_match_each_recorded_contract_item(monkeypatch):
    baseline = json.loads(FIXTURE.read_text())
    assert baseline["commit"] == BASE_SHA
    monkeypatch.setattr(lanes_settings, "enabled", lambda: True)
    actual = capture(monkeypatch)
    assert actual.keys() == baseline["items"].keys()
    for item, fields in baseline["items"].items():
        if item == "route_arrival":
            for row, expected in zip(actual[item], fields, strict=True):
                assert row == expected
        else:
            for field, expected in fields.items():
                assert actual[item][field] == expected, (item, field)


def test_disabled_decorators_touch_no_admission_storage_or_lock(monkeypatch):
    from looper.agents import turn_admission
    from looper.db import connection, provider_admission, provider_limits, schedules

    monkeypatch.setattr(admission._controller, "active", False)
    forbidden = Mock(side_effect=AssertionError("disabled turn touched admission state"))
    for module in (connection, provider_admission, provider_limits, schedules):
        monkeypatch.setattr(module, "get_pool", forbidden)
    monkeypatch.setattr(admission._controller, "lock", SimpleNamespace(__aenter__=forbidden))
    monkeypatch.setattr(admission, "worker_lane", forbidden)
    monkeypatch.setattr(admission, "slot", forbidden)

    @turn_admission.admitted_manager_turn
    async def manager_turn(agent, token, prepared):
        return prepared

    @turn_admission.admitted_planner_exchange
    async def planner_exchange(agent, text, process, limit, **kwargs):
        return text

    async def run():
        assert await manager_turn(object(), object(), "unchanged") == "unchanged"
        assert await planner_exchange(object(), "unchanged", object(), 30) == "unchanged"
    asyncio.run(run())
    forbidden.assert_not_called()
