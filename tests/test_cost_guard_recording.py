"""Every model turn reserves through the recorder, without changing its launch."""

from __future__ import annotations

import asyncio
import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
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


# Not marked provenance: this is the off/observe comparison itself, not a proof of where a fixture came from.
@pytest.mark.parametrize("role", ["manager", "planner", "schedule", "processor"])
@pytest.mark.parametrize("mode", ["off", "observe"])
def test_record_payload_is_byte_identical_to_the_pinned_main_recorder(monkeypatch, tmp_path, role, mode):
    """Read the actual base implementation, then compare complete would-be stored rows without running a model."""
    from looper.agents import claude_cli, cost_guard_settings
    from looper.agents.agent_settings import AgentType
    from looper.agents.system_prompt import SystemPrompt
    from tests.runtime_model_helpers import builtin_launch

    root = Path(__file__).resolve().parents[1]
    revision = "6613380fa"
    source = subprocess.check_output(
        ["git", "show", f"{revision}:looper/agents/claude_cli.py"], cwd=root, text=True)
    original = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == "ClaudeRunRecorder")
    namespace = dict(vars(claude_cli))
    exec(compile("from __future__ import annotations\n" + ast.get_source_segment(source, original),
                 f"{revision}:looper/agents/claude_cli.py", "exec"), namespace)
    baseline = namespace["ClaudeRunRecorder"]
    config = cost_guard_settings.Config(mode=cost_guard_settings.Mode(mode))
    monkeypatch.setattr(cost_guard_settings, "_config", config)
    monkeypatch.setattr(cost_guard, "_guard", cost_guard.Guard(config, clock=lambda: datetime(2026,11,23,tzinfo=timezone.utc)))
    monkeypatch.setattr(cost_guard.store, "history", AsyncMock(return_value=[]))
    monkeypatch.setattr(cost_guard.store, "limits", AsyncMock(return_value={}))
    monkeypatch.setattr(cost_guard.store, "context", AsyncMock(return_value={'tasks':{},'schedules':{}}))
    from looper.agents import session_budget
    monkeypatch.setattr(session_budget, "turn_budget_usd", AsyncMock(return_value=None))
    opened = AsyncMock(return_value=42)
    monkeypatch.setattr(llm_records, "start_record", opened)
    prompt = SystemPrompt(f"Synthetic {role} system prompt", {"role": {"id": role}})
    launch = builtin_launch(role=AgentType("planner" if role == "schedule" else role))
    command = launch.runtime.run_command("claude", cwd=tmp_path, prompt_file=tmp_path / "prompt.md", launch=launch)
    kwargs = {"agent_id": f"{role}-synthetic", "task_id": None if role == "manager" else "task-synthetic",
              "command": command, "cwd": str(tmp_path), "input_text": "Synthetic user turn",
              "session_id": "synthetic-session", "system_prompt": prompt, "launch": launch, "pid": 1000,
              **({'schedule_id':'schedule-synthetic'} if role == 'schedule' else {})}

    async def recorded(recorder_type):
        recorder = recorder_type(**kwargs)
        try:
            await recorder.start()
            payload = dict(opened.await_args.kwargs)
            if mode == 'observe':
                payload['metadata'] = llm_records.normalize_record_metadata(payload['metadata'])
            return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
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
    assert events[-1][1:] == ((42,), {"cost_usd": 0.25, "cost_source": "runtime", "model": None, "unknown_models": ()})


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


@pytest.mark.parametrize('mode', ['off', 'observe', 'enforce'])
@pytest.mark.parametrize('scope_cap', [None, 0, 2])
def test_subscription_reservation_adds_an_in_run_cap_only_with_an_enforced_scope_cap(monkeypatch, mode, scope_cap):
    from looper.agents.cost_guard_settings import Config, Mode
    from tests.runtime_model_helpers import builtin_launch
    launch = builtin_launch()
    assert cost_guard.currency_of(launch) is cost_guard.Currency.EQUIVALENT
    guard = cost_guard.Guard(Config(mode=Mode(mode)))
    monkeypatch.setattr(cost_guard, '_guard', guard)
    scopes = (cost_guard.Scope(cost_guard.BudgetScope.GLOBAL_DAY, 'global', scope_cap),)
    reservation = cost_guard.Reservation(.25, cost_guard.Currency.EQUIVALENT, scopes, {},
                                          datetime.now(timezone.utc))
    monkeypatch.setattr(cost_guard, 'before_run', AsyncMock(return_value=reservation if mode!='off' else None))
    monkeypatch.setattr(cost_guard, 'bind', AsyncMock())
    monkeypatch.setattr(llm_records, 'start_record', AsyncMock(return_value=42))
    recorder = ClaudeRunRecorder(agent_id='planner-synthetic',task_id='task-synthetic',command=['claude'],
        cwd='/synthetic',input_text='Synthetic',launch=launch)
    asyncio.run(recorder.start())
    try:
        if mode=='enforce' and scope_cap is not None:
            assert recorder._spend is not None and recorder._spend.cap==.25
        else:
            assert recorder._spend is None
    finally:
        recorder._untrack()


@pytest.mark.parametrize('cap_owner', ['manager', 'guard'])
@pytest.mark.parametrize('main_model', ['sonnet', 'opus'])
def test_runtime_bill_survives_an_unknown_subagent_estimate(agent_db, tmp_path, monkeypatch, cap_owner, main_model, caplog):
    from looper.agents import session_budget
    from looper.agents.agent_settings import AgentSettings, AgentType, Launch
    from looper.agents.cost_guard_settings import Config, Mode
    from looper.agents.profile_registry import builtin_resolved_profile
    from looper.agents.run_usage import Usage
    from looper.agents.runtimes.pricing import usage_event
    from looper.agents.tiers import manager_budget
    from tests.runtime_model_helpers import with_model_name

    launch = Launch(AgentSettings(profile=with_model_name(builtin_resolved_profile(), main_model)),
                    role=AgentType.MANAGER)
    config = Config(mode=Mode.ENFORCE if cap_owner == 'guard' else Mode.OBSERVE,
                    daily_equivalent_usd=4)
    guard = cost_guard.Guard(config, clock=lambda: datetime(2026, 11, 23, tzinfo=timezone.utc))
    monkeypatch.setattr(cost_guard, '_guard', guard)
    monkeypatch.setattr(guard, '_usage_hold', AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(cost_guard.store, 'history', AsyncMock(return_value=[]))
    monkeypatch.setattr(cost_guard.store, 'limits', AsyncMock(return_value={}))
    monkeypatch.setattr(cost_guard.store, 'context', AsyncMock(return_value={'tasks': {}, 'schedules': {}}))
    monkeypatch.setattr(session_budget, 'turn_budget_usd', AsyncMock(return_value=4 if cap_owner == 'manager' else None))

    async def run():
        session = SimpleNamespace(budget=None, pending_switch=None)
        options = await manager_budget.prepare(session, SimpleNamespace(), launch)
        recorder = ClaudeRunRecorder(agent_id='manager', task_id=None, command=['claude'], cwd=str(tmp_path),
                                     input_text='test', launch=launch, **options)
        await recorder.start()
        recorder.account(usage_event('claude-haiku-5-5', Usage(input_tokens=100)))
        assert recorder.cap_stop is None
        expected = 0.0002 if main_model == 'sonnet' else 0.0004
        assert recorder._spend.budget_cost_usd == pytest.approx(expected)
        assert not caplog.records
        await recorder.finish(output_text='done', result_event={
            'type': 'result', 'subtype': 'success', 'total_cost_usd': .05})

    asyncio.run(run())
    saved = agent_db.finish_record.await_args.kwargs
    assert saved['cost_source'] == 'runtime'
    assert saved['cost_usd'] == .05
    assert 'catalog_cost_status' not in saved.get('metadata', {})
    assert saved.get('metadata', {}).get('cost_status', 'known') == 'known'
    assert not saved.get('metadata', {}).get('unknown_price_models')
    counter = next(item for item in guard.snapshot()['counters'] if item['scope'] == 'global_day')
    assert counter['spent_usd'] == .05 and counter['cost_status'] == 'known'
    assert not caplog.records


def test_unknown_event_model_reaches_guard_settlement(monkeypatch):
    guard = cost_guard.Guard(cost_guard.settings.Config(mode=cost_guard.settings.Mode.OBSERVE))
    guard.day = guard.day_start(guard.clock())
    monkeypatch.setattr(cost_guard, '_guard', guard)
    scope = cost_guard.Scope(cost_guard.BudgetScope.GLOBAL_DAY, 'global', 4)
    reservation = guard._admit(cost_guard.RunKind.WORKER, (scope,), cost_guard.Currency.PROVIDER, .1,
                               cost_guard.EstimateSource.RUN_CAP, True, None).reservation
    guard.bind(reservation, 42)
    monkeypatch.setattr(llm_records, 'finish_record', AsyncMock())
    recorder = ClaudeRunRecorder(agent_id='worker-test', task_id=None, command=['claude'],
                                 cwd='/synthetic', input_text='test')
    recorder._record_id = 42
    asyncio.run(recorder._write_end({'status': 'completed', 'cost_usd': None, 'cost_source': 'provider_price',
                                     'model': 'sonnet', 'metadata': {
                                         'unknown_price_models': ['claude-haiku-5-5']}}))
    counter = guard.snapshot()['counters'][0]
    assert counter['cost_status'] == 'unknown'
    assert counter['unknown_models'] == ['claude-haiku-5-5']
