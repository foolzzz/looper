"""Fixed pre-admission bytes: launch, prompt and record contracts stay unchanged."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

from looper.agents import claude_cli, consult, limit_signals, processor
from looper.agents.agent_settings import Access, AgentType
from looper.agents.tiers import worker
from looper.db import llm_records, profile_registry, system_settings
from tests.runtime_model_helpers import builtin_launch

FIXTURE = Path(__file__).parent / "fixtures/admission_off.json"


def snapshot(monkeypatch):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))
    monkeypatch.setattr(claude_cli, "_clock", lambda: 100.0)
    saves = {}
    for name, result in (("start_record", 12), ("append_events", None), ("finish_record", None), ("touch_record", None)):
        saves[name] = AsyncMock(return_value=result)
        monkeypatch.setattr(llm_records, name, saves[name])
    monkeypatch.setattr(profile_registry, "list_providers", AsyncMock(return_value=[{
        "id": "account", "name": "Synthetic account", "auth": "oauth", "max_concurrency": 1}]))
    values = {"commands": [], "prompts": [worker.DEFAULT_SYSTEM_PROMPT, processor.PROCESSOR_ROLE_PROMPT,
                                          consult.consult_prompt(worker.DEFAULT_SYSTEM_PROMPT)]}

    async def scenario():
        for role, access in ((AgentType.WORKER, None), (AgentType.REVIEWER, None),
                             (AgentType.WORKER, Access.CONSULT), (AgentType.PROCESSOR, None)):
            launch = builtin_launch(role=role, access=access)
            command = launch.runtime.run_command("claude", cwd=Path("/workspace"),
                prompt_file=Path("/runtime/prompt.md"), launch=launch, max_turns=5)
            values["commands"].append(command)
            recorder = claude_cli.ClaudeRunRecorder(agent_id=role.value + "-sample", task_id=None,
                command=command, cwd="/workspace", input_text="sample", launch=launch)
            monkeypatch.setattr(recorder, "_after_run", AsyncMock())
            await recorder.start()
            event = {"type": "result", "subtype": "success", "result": "done", "total_cost_usd": 0}
            await recorder.record(event)
            await recorder.finish(output_text="done", result_event=event)
        values["limits"] = await limit_signals.limits()

    asyncio.run(scenario())
    values["records"] = {name: [(c.args, c.kwargs) for c in save.await_args_list] for name, save in saves.items()}
    return {key: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            for key, value in values.items()}


def test_disabled_admission_matches_fixed_preimplementation_bytes(monkeypatch):
    from looper.agents import admission
    from looper.db import connection, provider_admission
    from unittest.mock import Mock

    monkeypatch.setattr(admission._controller, "active", False)
    pool = Mock(side_effect=AssertionError("disabled path queried admission storage"))
    monkeypatch.setattr(connection, "get_pool", pool)
    monkeypatch.setattr(provider_admission, "get_pool", pool)
    assert snapshot(monkeypatch) == json.loads(FIXTURE.read_text())["sha256"]
    pool.assert_not_called()


def test_disabled_task_slot_keeps_its_existing_wakeup_behavior(monkeypatch, tmp_path):
    from looper.agents import admission
    from looper.agents.tiers.worker import WorkerConfig, WorkerPool, WorkerProcess

    monkeypatch.setattr(admission._controller, "active", False)
    monkeypatch.setattr(worker, "load_task_limit", AsyncMock(return_value=1))
    forbidden = AsyncMock(side_effect=AssertionError("disabled task slot touched account admission"))
    monkeypatch.setattr(profile_registry, "get_provider", forbidden)

    async def scenario():
        pool = WorkerPool(config=WorkerConfig(), runtime_root=tmp_path)
        monkeypatch.setattr(pool, "_project_id", AsyncMock(return_value=None))
        monkeypatch.setattr(pool, "_activity", AsyncMock())
        one = WorkerProcess(worker_id="one", task_id="s1", plan_id="p", parent_task_id="task")
        two = WorkerProcess(worker_id="two", task_id="s2", plan_id="p", parent_task_id="task")
        await pool._take_slot(one)
        waiting = asyncio.create_task(pool._take_slot(two))
        for _ in range(10):
            await asyncio.sleep(0)
            if pool._slot_waiters:
                break
        assert not waiting.done() and pool._slot_waiters
        pool._slots.pop("one")
        pool._wake_slot_waiters()
        await asyncio.wait_for(waiting, .2)
        assert pool._slots == {"two": (two.role, "task")}
        forbidden.assert_not_awaited()

    # _take_slot itself is unchanged from the fixture's fixed preimplementation commit.
    asyncio.run(scenario())
