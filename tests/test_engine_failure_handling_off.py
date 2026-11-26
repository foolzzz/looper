"""Each disabled-path field is frozen against the pre-handling revision, rather than a combined hash.

Provenance capture runs this exact file in a read-only source archive of BASE_SHA. Normal tests only read the
fixture. Normalization is limited to test-owned paths, timestamps, generated UUIDs, record durations and PIDs;
input, output, failure fields, commands and metadata values are never removed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import pytest

from looper.agents import admission, claude_cli, failures, processor, single_run
from looper.agents.agent_settings import AgentType
from looper.agents.message import MessageEnvelope, MessageType, planner_address, worker_address
from looper.agents.ops import Turn, TurnOrigin
from looper.agents.ops.service import DEFAULT_SCOPE, MANAGER_TOOLS, ManagerOps
from looper.agents.run_usage import Usage
from looper.agents.system_prompt import SystemPrompt
from looper.agents.tiers import manager, planner, worker
from looper.agents.tiers.planner_protocol import DecisionContext, ResultKind, TurnKind
from looper.db import llm_records, system_settings
from tests.run_handle_support import ScriptedRun
from tests.runtime_model_helpers import builtin_launch

BASE_SHA = "11a10a8e4f1a57b071298b0a1fed75b42f47c3d8"
FIXTURE = Path(__file__).parent / "fixtures" / "engine_failure_handling_off.json"
OK = {"type": "result", "subtype": "success", "result": "synthetic reply", "total_cost_usd": 0.1}
BAD = {"type": "result", "subtype": "error_during_execution", "is_error": True,
       "api_error": "connection_failed", "result": "synthetic failure", "total_cost_usd": 0.1}


def _normalize(value, root):
    if isinstance(value, dict):
        return {key: ("<timestamp>" if key in {"created_at", "updated_at", "started_at", "finished_at", "since", "timestamp"}
                      and item is not None else "<pid>" if key == "pid" and item is not None else
                      _normalize(item, root)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, root) for item in value]
    if isinstance(value, datetime):
        return "<timestamp>"
    if isinstance(value, Path):
        value = str(value)
    if isinstance(value, str):
        return value.replace(str(root), "<test-root>")
    return value


def capture(monkeypatch, agent_db, root, *, mode="off"):
    """Exercise recorders, Worker result delivery, Planner exchanges and the processor's filtered real path."""
    monkeypatch.setattr(admission._controller, "active", False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(claude_cli, "_clock", lambda: 100.0)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=1))
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(claude_cli.ClaudeRunRecorder, "_after_run", AsyncMock())
    outside_memory = claude_cli._outside_memory

    def portable_memory(cwd, *, repository=True):
        try:
            logical = Path("/workspace") / Path(cwd).relative_to(root)
        except ValueError:
            logical = Path(cwd)
        return outside_memory(logical, repository=repository)

    monkeypatch.setattr(claude_cli, "_outside_memory", portable_memory)
    for name, answer in (("start_record", 12), ("append_events", None), ("finish_record", None),
                         ("touch_record", None), ("add_metadata", None)):
        monkeypatch.setattr(llm_records, name, AsyncMock(return_value=answer))
    output = {}

    def reset():
        for name in ("start_record", "append_events", "finish_record", "touch_record", "add_metadata"):
            getattr(llm_records, name).reset_mock()

    def records():
        return {name: [{"args": call.args, "fields": call.kwargs} for call in getattr(llm_records, name).await_args_list]
                for name in ("start_record", "append_events", "finish_record", "touch_record", "add_metadata")}

    from test_turn_admission_off import capture as turn_capture
    legacy = turn_capture(monkeypatch)
    legacy_starts = copy.deepcopy(llm_records.start_record.await_args_list)
    legacy_finishes = copy.deepcopy(llm_records.finish_record.await_args_list)
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value=(
        {failures.SETTING_KEY: "record"} if mode == "record" else {})))

    async def run():
        agent = planner.PlannerAgent(task_id="synthetic-task", runtime_root=root / "runtime")
        step = planner.PlanStep("synthetic-step", 0, "Inspect inputs", status="dispatched", worker_id="worker-1")
        plan = agent._plan = planner.TaskPlan("synthetic-plan", "synthetic-goal", "Inspect", "/workspace", [step],
                                              status="executing")
        launch = builtin_launch(role=AgentType.PLANNER)
        settings = planner.PlannerSettings(planner.DEFAULT_SYSTEM_PROMPT, 30, 5, 0)
        prompt = agent._system_prompt(plan, settings, launch.knowledge, launch.settings.harness)
        command = agent._claude_command(root / "runtime" / "planner.md",
            settings, launch, "/workspace")
        process = planner._Process(command, None, prompt, launch)
        event = OK

        async def turn(text, on_event, **kwargs):
            if text == "/compact":
                await on_event({"type": "system", "subtype": "compact_boundary",
                                "compact_metadata": {"trigger": "manual", "post_tokens": 10}})
            await on_event(event)
            return claude_cli.ResidentTurn(event, usage=Usage(cost_usd=0.1))

        agent._claude = NS(started=False, pid=None, totals=Usage(), run_turn=turn, stop=AsyncMock())
        for name in ("_read_run_deadline", "_read_roles", "_save_session", "_report_read_only"):
            monkeypatch.setattr(agent, name, AsyncMock())
        context = DecisionContext(step.step_id, ResultKind.WORK, False, False, 0)
        decision = agent._decision_message(plan, step, context, ["Worker failed: synthetic failure"])
        for name, kind, text, event in (
            ("planner_plan", TurnKind.PLAN, "[PLAN] Inspect the synthetic task", OK),
            ("planner_decision", TurnKind.DECISION, decision, OK),
            ("planner_chat", TurnKind.CHAT, "[CHAT] Explain the result", OK),
            ("planner_compact", None, "/compact", OK),
            ("planner_plan_failed", TurnKind.PLAN, "[PLAN] Inspect the synthetic task", BAD),
            ("planner_decision_failed", TurnKind.DECISION, decision, BAD),
            ("planner_chat_failed", TurnKind.CHAT, "[CHAT] Explain the result", BAD)):
            reset()
            async def exchange(message):
                return await agent._exchange(message, process, 30, maintenance=kind is None)
            error = None
            try:
                reply = await exchange(text) if kind is None else await agent._ask(kind, text, str, run=exchange)
            except planner.PlannerTurnError as exc:
                reply, error = None, str(exc)
            output[name] = {"system_prompt": prompt.text, "contract": planner.reply_contract(),
                            "records": records(), "reply": reply, "error": error, "command": command,
                            "plan_status": plan.status, "step_status": step.status, "retries": step.retries}

        # The Manager's existing admission capture exercises real turn setup and bus notifications. Add every
        # recorded value to its per-turn snapshots; its former metadata-keys-only field is not used here.
        names = ["planner_plan", "planner_chat", "planner_compact", "manager_user", "manager_completion"]
        starts, finishes = legacy_starts, legacy_finishes
        for index, name in enumerate(names):
            if not name.startswith("manager"):
                continue
            output[name] = {**legacy[name], "start_record": copy.deepcopy(starts[index].kwargs),
                            "finish_record": copy.deepcopy(finishes[index].kwargs)}
            del output[name]["metadata_keys"]
        for name, text in (("manager_user_failed", "Inspect the synthetic task"),
                           ("manager_completion_failed", "[COMPLETION] Looper reports tasks that ended.")):
            reset()
            from test_turn_admission_off import _envelope, _token
            from looper.db import manager_events

            agent = manager.ManagerAgent(config=manager.ManagerConfig(work_dir=Path("/workspace")),
                                         runtime_root=root / "runtime", clock=lambda: 100.0)
            sent = []
            agent._bus = NS(publish=AsyncMock(side_effect=lambda envelope: sent.append(asdict(envelope))))
            launch = builtin_launch(role=AgentType.MANAGER)
            prompt = agent._composed_prompt(launch.settings)
            command = launch.runtime.resident_command("synthetic-runtime", cwd="/workspace",
                prompt_file=Path("/runtime/manager.md"), launch=launch)
            origin = TurnOrigin.COMPLETION if "completion" in name else TurnOrigin.USER
            token = _token(origin)
            envelope = _envelope()
            prepared = manager.Prepared(envelope=envelope, turn=token.ops_turn, message=text, turn_text=text,
                system_prompt=prompt, launch=launch, session=agent._sessions["main"])

            async def start_turn(given_token, given):
                given.run.command = command
                given.run.recorder = claude_cli.ClaudeRunRecorder(agent_id="manager", task_id=None, command=command,
                    cwd="/workspace", input_text=text, system_prompt=prompt, launch=launch)
                await given.run.recorder.start()

            async def converse(text, on_event, **kwargs):
                await on_event(BAD)
                return claude_cli.ResidentTurn(BAD, usage=Usage(cost_usd=.1))

            agent._claude = NS(started=True, pid=None, shares_bridge=False, totals=Usage(),
                               run_turn=converse, stop=AsyncMock())
            monkeypatch.setattr(agent, "_start_turn", start_turn)
            monkeypatch.setattr(agent, "_prepare_turn", AsyncMock(return_value=prepared))
            monkeypatch.setattr(agent._router, "release", AsyncMock())
            monkeypatch.setattr(agent, "_save_session_state", AsyncMock())
            fallback = AsyncMock()
            if origin is TurnOrigin.USER:
                await agent._answer_user_message_impl(envelope, token)
            else:
                row = {"id": 1, "manager_id": "manager", "task_id": "synthetic-task", "kind": "task",
                       "target_kind": "web", "channel_id": "channel-web", "chat": "sample", "payload": {},
                       "fallback": [{"via": "web_thread", "text": "Task ended.", "thread": "sample",
                                     "origin_channel_id": "channel-web"}], "delivery_claim": "synthetic-claim"}
                fallback = AsyncMock(return_value=[row])
                monkeypatch.setattr(manager_events, "fall_back", fallback)
                monkeypatch.setattr(manager_events, "claim_deliveries", AsyncMock(return_value=[row]))
                monkeypatch.setattr(manager_events, "settle_delivery", AsyncMock())
                monkeypatch.setattr(manager_events, "failing", AsyncMock(return_value=False))

                async def completion(rows, batch, state):
                    state.update(token=token, turn=token.ops_turn)
                    return await agent._run_prepared(token, prepared)

                monkeypatch.setattr(agent, "_completion_turn", completion)
                await agent._tell_completion([row], uuid.UUID(int=1))
            output[name] = {"system_prompt": prompt.text, "records": records(), "command": command,
                            "input_text": text, "envelopes": sent,
                            "fallback": [{"args": call.args, "fields": call.kwargs} for call in fallback.await_args_list]}
        ops = ManagerOps(NS(), url="http://127.0.0.1:1", token="synthetic-token")
        monkeypatch.setattr(ops, "current_scope", AsyncMock(return_value=await ops._with_operator_tools(DEFAULT_SCOPE)))
        output["manager_tools"] = {"definitions": [tool.listing() for tool in MANAGER_TOOLS],
            "user_tools_list": await ops.current_listing(Turn(channel="web", chat="synthetic", origin=TurnOrigin.USER)),
            "completion_tools_list": await ops.current_listing(Turn(channel="web", chat="synthetic", origin=TurnOrigin.COMPLETION))}

        from test_worker_run_handles import _pool, _item
        for name, review, event, attempt in (
            ("worker_first", False, {**OK, "result": json.dumps({"status": "completed", "summary": "done", "changed_files": {}})}, 1),
            ("worker_failed", False, BAD, 1), ("worker_retry", False, BAD, 2),
            ("reviewer_passed", True, {**OK, "result": json.dumps({"passed": True, "summary": "checked", "findings": []})}, 1),
            ("reviewer_not_passed", True, {**OK, "result": json.dumps({"passed": False, "summary": "needs repair", "findings": ["missing check"]})}, 1),
            ("reviewer_failed", True, BAD, 1)):
            with monkeypatch.context() as patch:
                reset()
                run = ScriptedRun([event])
                async with _pool(root, patch, run) as (pool, launches, results):
                    task_section = pool._task_section
                    patch.setattr(pool, "_task_section", lambda *args, **kwargs:
                                  task_section(*args, **kwargs).replace(str(root), "/workspace"))
                    if review:
                        pool._bus.subscribe_topic(MessageType.VERIFY_RESULT, results.append)
                    envelope = _item(root)
                    envelope = replace(envelope, message_id="synthetic-dispatch", correlation_id="synthetic-correlation",
                        message_type=MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM,
                        payload={**envelope.payload, "attempt": attempt, "feedback": ["Check the prior result"] if attempt > 1 else [],
                                 "work_result": {"summary": "done", "changed_files": {}}} if review else
                                {**envelope.payload, "attempt": attempt, "feedback": ["Check the prior result"] if attempt > 1 else []})
                    await pool._dispatch(envelope)
                    await pool._tasks["w1"]
                    await asyncio.sleep(0)
                    output[name] = {"records": records(), "turn_text": run.prompt,
                        "launches": [{key: launch[key] for key in
                                      ("cwd", "prompt_file", "max_turns", "timeout_s", "session_dir", "tools")}
                                     for launch in launches],
                        "envelopes": [asdict(item) for item in results], "slots": dict(pool._slots)}

        for name, review in (("kept_worker_failed", False), ("kept_reviewer_failed", True)):
            with monkeypatch.context() as patch:
                reset()
                run = ScriptedRun([BAD])
                run.state = worker.KeptRunState.ATTACHED
                run.replay_finished = asyncio.Event()
                run.replay_finished.set()
                async with _pool(root, patch, run) as (pool, launches, results):
                    if review:
                        pool._bus.subscribe_topic(MessageType.VERIFY_RESULT, results.append)
                    section = pool._task_section
                    patch.setattr(pool, "_task_section", lambda *args, **kwargs:
                                  section(*args, **kwargs).replace(str(root), "/workspace"))
                    envelope = replace(_item(root), message_id="synthetic-dispatch", correlation_id="synthetic-correlation",
                        message_type=MessageType.VERIFY_REQUEST if review else MessageType.WORK_ITEM)
                    ctx = await pool._prepare(envelope, is_verify=review)
                    wp = worker.WorkerProcess("w1", "s1", "p", role=AgentType.REVIEWER if review else AgentType.WORKER,
                                              parent_task_id="t1", handle=run)
                    recorder = claude_cli.ClaudeRunRecorder(agent_id="w1", task_id="t1", command=ctx.command,
                        cwd=ctx.work_dir, input_text=ctx.prompt, step_id="s1", system_prompt=ctx.system_prompt,
                        launch=ctx.launch, whole_process=True)
                    await pool._run_adopted(wp, ctx, run, recorder,
                        worker.RunReply(envelope.source, envelope.correlation_id, {}), [])
                    output[name] = {"records": records(), "envelopes": [asdict(item) for item in results],
                                    "launch_calls": run.launch_calls, "worker_status": wp.status}

        # The actual processor keeps its own validation, command, record and privacy filter. Only the runtime's
        # stream producer is replaced; the raw rate signal is observed before typed_events filters it out.
        settings = builtin_launch(role=AgentType.PROCESSOR).settings
        monkeypatch.setattr(processor, "load_agent_settings", AsyncMock(return_value=settings))
        monkeypatch.setattr(processor, "_managed_hooks", lambda settings: None)
        monkeypatch.setattr(processor, "_role", AsyncMock(return_value=processor._Role("Process messages.", False, 0, 5, True)))
        monkeypatch.setattr(llm_records, "get_record", AsyncMock(return_value={"cost_usd": 0.1}))
        for name, event in (("processor_success", OK), ("processor_failed", BAD)):
            reset()
            async def run_once(recorder, command, **kwargs):
                await recorder.record({"type": "rate_limit_event", "rate_limit_info": {
                    "status": "rejected", "rateLimitType": "seven_day", "resetsAt": 1800000000}})
                await recorder.record(event)
                await recorder.finish(output_text=None if event.get("is_error") else event["result"],
                                      result_event=event, error="its run ended in error" if event.get("is_error") else None)
                return single_run.Finished(event, True, False)
            monkeypatch.setattr(single_run, "run_once", run_once)
            result = await processor.run(owner="synthetic-batch", instructions="Summarize incidents.",
                input_text="private synthetic batch", input_ref={"from_seq": 1, "to_seq": 3}, quiet_word="NONE",
                budget_usd=1.0, timeout_s=30, runtime_root=root / "runtime", claude_bin="synthetic-runtime")
            output[name] = {"records": records(), "result": {field: getattr(result, field) for field in
                ("reply", "record_id", "cost_usd", "failure", "detail", "profile")},
                "contract": processor.reply_contract("NONE"), "raw_input": processor.user_turn("private synthetic batch")}

    asyncio.run(run())
    return _normalize(output, root)


def test_off_matches_each_frozen_item(monkeypatch, agent_db, tmp_path):
    from looper.agents import failure_handling
    from looper.db import step_failures

    forbidden = Mock(side_effect=AssertionError("disabled handling touched the act machinery"))
    monkeypatch.setattr(failures, "classify", forbidden)
    monkeypatch.setattr(step_failures, "get_pool", forbidden)
    for name in ("handle_step_result", "note_success", "restore_task", "_schedule"):
        monkeypatch.setattr(failure_handling.FailureHandlingService, name, forbidden)
    expected = json.loads(FIXTURE.read_text())
    assert expected["commit"] == BASE_SHA
    actual = capture(monkeypatch, agent_db, tmp_path)
    # Delivery observation adds a JSON-safe tag to the fallback, without changing failure handling.
    assert [envelope["metadata"].pop("proactive")
            for envelope in actual["manager_completion_failed"]["envelopes"]] == [
        {"source": "task_notice", "source_ref": "synthetic-task", "manager_id": "manager"}]
    assert actual.keys() == expected["items"].keys()
    for item, fields in expected["items"].items():
        assert actual[item].keys() == fields.keys(), item
        for field, value in fields.items():
            assert json.loads(json.dumps(actual[item][field], default=str)) == value, (item, field)
    forbidden.assert_not_called()


def test_record_keeps_existing_control_flow(monkeypatch, agent_db, tmp_path):
    with monkeypatch.context() as patch:
        off = capture(patch, agent_db, tmp_path)
    with monkeypatch.context() as patch:
        recorded = capture(patch, agent_db, tmp_path, mode="record")
    classes = []
    for name, item in recorded.items():
        if "records" not in item:
            assert item == off[name], name
            continue
        for close in item["records"]["finish_record"]:
            fields = close["fields"]
            if "failure_class" in fields:
                classes.append(fields.pop("failure_class"))
                assert fields.pop("failure_cause") in ("connection_failed", "usage_limit")
                fields["metadata"].pop("failure")
                if not fields["metadata"]:
                    fields.pop("metadata")
        for field in item:
            assert json.loads(json.dumps(item[field], default=str)) == json.loads(json.dumps(off[name][field], default=str)), (name, field)
    assert set(classes) == {"transient", "quota"}


@pytest.mark.provenance
def test_capture_baseline_provenance(monkeypatch, agent_db, tmp_path):
    import looper

    destination = os.environ.get("FAILURE_FREEZE_OUTPUT")
    if not destination:
        pytest.skip("baseline capture is an explicit provenance operation")
    source = Path(os.environ["FAILURE_FREEZE_SOURCE"]).resolve()
    assert Path(looper.__file__).resolve().is_relative_to(source)
    assert os.environ["FAILURE_FREEZE_SOURCE_SHA"] == BASE_SHA
    value = {"commit": BASE_SHA, "normalization": "Only test-owned paths, timestamps, generated UUIDs and PIDs.",
             "items": capture(monkeypatch, agent_db, tmp_path)}
    Path(destination).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
