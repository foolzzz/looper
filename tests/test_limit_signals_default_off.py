"""A captured main baseline guards prompts, tools, commands and recorded bytes with signals off."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

from looper.agents import agent_settings, claude_cli
from looper.agents.ops import clients, planner, room, service
from looper.agents.tiers import manager, planner as planner_tier, worker
from looper.db import llm_records, system_settings
from tests.runtime_model_helpers import builtin_launch


def snapshot(monkeypatch):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))
    monkeypatch.setattr(claude_cli, "_clock", lambda: 100.0)
    saved = {}
    for name, value in (("start_record", 12), ("append_events", None), ("finish_record", None), ("touch_record", None)):
        saved[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(llm_records, name, saved[name])
    launch = builtin_launch()
    commands = []
    for role in ("manager", "planner", "worker"):
        selected = builtin_launch(role=agent_settings.AgentType(role))
        commands.append(selected.runtime.run_command("claude", cwd=Path("/workspace"),
                        prompt_file=Path("/runtime/prompt.md"), launch=selected, max_turns=5))
    events = [{"type": "system", "subtype": "init", "model": "sample"},
              {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day"}},
              {"type": "result", "subtype": "success", "result": "done", "total_cost_usd": 0}]
    async def record():
        recorder = claude_cli.ClaudeRunRecorder(agent_id="worker-sample", task_id=None,
            command=commands[-1], cwd="/workspace", input_text="sample", launch=launch)
        monkeypatch.setattr(recorder, "_after_run", AsyncMock())
        await recorder.start()
        for event in events:
            await recorder.record(event)
        await recorder.finish(output_text="done", result_event=events[-1])
    asyncio.run(record())
    values = {
        "prompts": [manager.DEFAULT_SYSTEM_PROMPT, planner_tier.DEFAULT_SYSTEM_PROMPT,
                    worker.DEFAULT_SYSTEM_PROMPT, agent_settings.DEFAULT_HARNESS_PROMPT],
        "tools": [(t.name, t.description, t.schema) for t in (*service.MANAGER_TOOLS, *clients.CLIENT_TOOLS,
                   *planner.TOOLS, *planner.ROLE_TOOLS, *room.TOOLS)],
        "commands": commands,
        "records": {name: [(call.args, call.kwargs) for call in mock.await_args_list] for name, mock in saved.items()},
    }
    return {name: hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            for name, value in values.items()}


def test_disabled_signals_match_main_bytes(monkeypatch):
    baseline = json.loads((Path(__file__).parent / "fixtures/limit_signals/default_off.json").read_text())
    assert snapshot(monkeypatch) == baseline["sha256"]
