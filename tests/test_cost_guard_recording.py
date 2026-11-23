"""Every model turn reserves through the recorder, without changing its launch."""

from __future__ import annotations

import asyncio
import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper.agents import cost_guard
from looper.agents.claude_cli import ClaudeRunRecorder
from looper.db import llm_records


@pytest.mark.parametrize("kind", ["manager", "planner", "worker", "reviewer", "processor", "leads_check",
                                  "advisor", "advice_check", "outbound_check", "consult"])
def test_each_model_turn_reserves_before_its_record_opens(monkeypatch, kind):
    reservation = SimpleNamespace(metadata=lambda: {"reservation": {"usd": 1, "currency": "equivalent_usd"}})
    events = []

    async def reserve(**kwargs):
        events.append(("reserve", kwargs))
        return reservation

    async def opened(**kwargs):
        events.append(("record", kwargs))
        return 42

    bound = AsyncMock()
    monkeypatch.setattr(cost_guard, "before_run", reserve)
    monkeypatch.setattr(cost_guard, "bind", bound)
    monkeypatch.setattr(llm_records, "start_record", opened)
    recorder = ClaudeRunRecorder(agent_id=f"{kind}-synthetic", task_id="task-synthetic", command=["claude"],
                                 cwd="/synthetic", input_text="hello", run_kind=kind,
                                 schedule_id="schedule-synthetic")
    asyncio.run(recorder.start())
    assert [event[0] for event in events] == ["reserve", "record"]
    assert events[0][1] == {"launch": None, "run_kind": kind, "task_id": "task-synthetic",
                            "schedule_id": "schedule-synthetic"}
    assert events[1][1]["metadata"]["reservation"] == reservation.metadata()["reservation"]
    bound.assert_awaited_once_with(reservation, 42)
    recorder._untrack()


def test_off_keeps_the_original_stored_fields(monkeypatch):
    monkeypatch.setattr(cost_guard, "before_run", AsyncMock(return_value=None))
    bound = AsyncMock()
    monkeypatch.setattr(cost_guard, "bind", bound)
    opened = AsyncMock(return_value=42)
    monkeypatch.setattr(llm_records, "start_record", opened)
    recorder = ClaudeRunRecorder(agent_id="worker-synthetic", task_id="task-synthetic", command=["claude"],
                                 cwd="/synthetic", input_text="hello")
    asyncio.run(recorder.start())
    # This is the complete original start_record payload for a recorder without a Launch.
    assert opened.await_args.kwargs == {
        "agent_id": "worker-synthetic", "task_id": "task-synthetic", "session_id": None, "input_text": "hello",
        "run_kind": None, "parent_record_id": None, "metadata": {"command": ["claude"], "cwd": "/synthetic"}}
    bound.assert_not_awaited()
    recorder._untrack()


# Not marked provenance: this is the off-path comparison itself, not a proof of where a fixture came from.
@pytest.mark.parametrize("role", ["manager", "planner", "processor"])
def test_off_record_payload_is_byte_identical_to_the_pinned_main_recorder(monkeypatch, tmp_path, role):
    """Read the actual base implementation, then compare complete would-be stored rows without running a model."""
    from looper.agents import claude_cli, cost_guard_settings
    from looper.agents.agent_settings import AgentType
    from looper.agents.system_prompt import SystemPrompt
    from tests.runtime_model_helpers import builtin_launch

    root = Path(__file__).resolve().parents[1]
    revision = "4b602aeed"
    source = subprocess.check_output(
        ["git", "show", f"{revision}:looper/agents/claude_cli.py"], cwd=root, text=True)
    original = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == "ClaudeRunRecorder")
    namespace = dict(vars(claude_cli))
    exec(compile("from __future__ import annotations\n" + ast.get_source_segment(source, original),
                 f"{revision}:looper/agents/claude_cli.py", "exec"), namespace)
    baseline = namespace["ClaudeRunRecorder"]
    monkeypatch.setattr(cost_guard_settings, "_config", cost_guard_settings.Config())
    monkeypatch.setattr(cost_guard, "_guard", cost_guard.Guard(cost_guard_settings.Config()))
    opened = AsyncMock(return_value=42)
    monkeypatch.setattr(llm_records, "start_record", opened)
    prompt = SystemPrompt(f"Synthetic {role} system prompt", {"role": {"id": role}})
    launch = builtin_launch(role=AgentType(role))
    command = launch.runtime.run_command("claude", cwd=tmp_path, prompt_file=tmp_path / "prompt.md", launch=launch)
    kwargs = {"agent_id": f"{role}-synthetic", "task_id": None if role == "manager" else "task-synthetic",
              "command": command, "cwd": str(tmp_path), "input_text": "Synthetic user turn",
              "session_id": "synthetic-session", "system_prompt": prompt, "launch": launch, "pid": 1000}

    async def recorded(recorder_type):
        recorder = recorder_type(**kwargs)
        try:
            await recorder.start()
            return json.dumps(opened.await_args.kwargs, sort_keys=True, separators=(",", ":")).encode()
        finally:
            recorder._untrack()

    async def compare():
        expected = await recorded(baseline)
        actual = await recorded(ClaudeRunRecorder)
        assert actual == expected

    asyncio.run(compare())


@pytest.mark.parametrize("kind", ["worker", "outbound_check"])
def test_failed_reservation_persistence_never_runs_an_outbound_classifier(monkeypatch, kind):
    reservation = SimpleNamespace(metadata=lambda: {"reservation": {"usd": 0.1}})
    monkeypatch.setattr(cost_guard, "before_run", AsyncMock(return_value=reservation))
    monkeypatch.setattr(llm_records, "start_record", AsyncMock(side_effect=OSError("synthetic database failure")))
    abandoned, bound = AsyncMock(), AsyncMock()
    monkeypatch.setattr(cost_guard, "abandon", abandoned)
    monkeypatch.setattr(cost_guard, "bind", bound)
    recorder = ClaudeRunRecorder(agent_id=f"{kind}-synthetic", task_id=None, command=["claude"], cwd="/synthetic",
                                 input_text="hello", run_kind=kind)
    if kind == "outbound_check":
        with pytest.raises(RuntimeError, match="reservation"):
            asyncio.run(recorder.start())
    else:
        asyncio.run(recorder.start())
    abandoned.assert_awaited_once_with(reservation)
    bound.assert_not_awaited()
    assert recorder.record_id is None
    recorder._untrack()


def test_outbound_reservation_error_is_outside_observability_error_handler(monkeypatch):
    monkeypatch.setattr(cost_guard, "before_run", AsyncMock(side_effect=RuntimeError("synthetic reservation error")))
    opened = AsyncMock()
    monkeypatch.setattr(llm_records, "start_record", opened)
    recorder = ClaudeRunRecorder(agent_id="outbound_check-synthetic", task_id=None, command=["claude"],
                                 cwd="/synthetic", input_text="hello", run_kind="outbound_check")
    with pytest.raises(RuntimeError, match="reservation"):
        asyncio.run(recorder.start())
    opened.assert_not_awaited()


def test_finished_record_replaces_reservation_only_after_its_cost_is_saved(monkeypatch):
    events = []

    async def saved(*args, **kwargs):
        events.append(("record", args, kwargs))

    async def settled(*args, **kwargs):
        events.append(("settle", args, kwargs))

    monkeypatch.setattr(llm_records, "finish_record", saved)
    monkeypatch.setattr(cost_guard, "finish", settled)
    recorder = ClaudeRunRecorder(agent_id="worker-synthetic", task_id=None, command=["claude"], cwd="/synthetic",
                                 input_text="hello")
    recorder._record_id = 42
    asyncio.run(recorder._write_end({"status": "completed", "cost_usd": 0.25, "cost_source": "runtime"}))
    assert [event[0] for event in events] == ["record", "settle"]
    assert events[-1][1:] == ((42,), {"cost_usd": 0.25, "cost_source": "runtime"})


@pytest.mark.parametrize('failure', ['guard', 'reservation', 'audit', 'guard_audit', None])
@pytest.mark.parametrize('verdict', ['allow', 'block'])
def test_review_allows_only_a_successful_classifier_and_a_successful_audit(monkeypatch, tmp_path, failure, verdict):
    from looper.agents import outbound
    from looper.agents.agent_settings import AgentType
    from looper.agents.cost_guard_settings import Config, Mode
    from looper.db import outbound_reviews
    from tests.runtime_model_helpers import builtin_launch

    g = cost_guard.Guard(Config(mode=Mode.OBSERVE))
    monkeypatch.setattr(cost_guard, '_guard', g)
    monkeypatch.setattr(cost_guard.store, 'history', AsyncMock(
        side_effect=OSError('synthetic guard failure') if failure in ('guard', 'guard_audit') else None, return_value=[]))
    monkeypatch.setattr(cost_guard.store, 'limits', AsyncMock(return_value={}))
    monkeypatch.setattr(llm_records, 'start_record', AsyncMock(
        side_effect=OSError('synthetic reservation failure') if failure == 'reservation' else None, return_value=42))
    audit = AsyncMock(side_effect=OSError('synthetic audit failure') if failure in ('audit', 'guard_audit') else None,
                      return_value=7)
    monkeypatch.setattr(outbound_reviews, 'insert', audit)
    classified = []

    async def classify(*args):
        recorder = ClaudeRunRecorder(agent_id='outbound_check-synthetic', task_id=None, command=['claude'],
                                     cwd=str(tmp_path), input_text='synthetic action', run_kind='outbound_check',
                                     launch=builtin_launch(role=AgentType.OUTBOUND_CHECK))
        try:
            await recorder.start()
            classified.append(verdict)
            return outbound.Decision(verdict), 'Synthetic classifier verdict.', (), None
        finally:
            recorder._untrack()

    monkeypatch.setattr(outbound, '_classify', classify)
    action = outbound.Action('synthetic_send', outbound.ActionLevel.EXTERNAL, 'chat', 'synthetic', 'Send example')
    origin = outbound.Origin('manager', outbound.Source.TOOL)
    async def check():
        # A failed first rebuild leaves day unset; later reviews must still classify rather than repeatedly block.
        for _ in range(2):
            result = await outbound.review(action, outbound.Asker(outbound.Who.OPERATOR, message='Send example'),
                                           origin, runtime_root=tmp_path)
            assert result.allowed is (verdict == 'allow' and failure in (None, 'guard'))
        assert classified == ([] if failure == 'reservation' else [verdict, verdict])
        assert audit.await_count == 2
        if failure in ('guard', 'guard_audit'):
            assert g.problem and g.day is None
            assert cost_guard.store.history.await_count == 2
    asyncio.run(check())
