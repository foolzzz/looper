"""Signals off add nothing to the prompts, tools, commands or recorded bytes main already had.

Prompts and tools are the shared per-item catalog. Each role's command and each
recorder call list is frozen on its own (``fixtures/limit_signals/default_off.json``).
A prompt section, tool or recorder added since is not compared.
"""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

from looper.agents import agent_settings, claude_cli
from looper.db import llm_records, system_settings
from tests.runtime_model_helpers import builtin_launch
from tests.test_intents_default_off import assert_default_prompts_and_tools


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _frozen(current: dict, baseline: dict) -> None:
    assert [key for key in current if key in baseline["each"]] == baseline["order"]
    assert {key: _sha(current[key]) if key in current else None for key in baseline["each"]} == baseline["each"]


def captured(monkeypatch):
    monkeypatch.setattr(system_settings, "get_settings", AsyncMock(return_value={}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))
    monkeypatch.setattr(claude_cli, "_clock", lambda: 100.0)
    saved = {}
    for name, value in (("start_record", 12), ("append_events", None), ("finish_record", None), ("touch_record", None)):
        saved[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(llm_records, name, saved[name])
    launch = builtin_launch()
    commands = {}
    for role in ("manager", "planner", "worker"):
        selected = builtin_launch(role=agent_settings.AgentType(role))
        commands[role] = selected.runtime.run_command(
            "claude", cwd=Path("/workspace"), prompt_file=Path("/runtime/prompt.md"), launch=selected, max_turns=5)
    events = [{"type": "system", "subtype": "init", "model": "sample"},
              {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day"}},
              {"type": "result", "subtype": "success", "result": "done", "total_cost_usd": 0}]

    async def record():
        recorder = claude_cli.ClaudeRunRecorder(agent_id="worker-sample", task_id=None,
            command=commands["worker"], cwd="/workspace", input_text="sample", launch=launch)
        monkeypatch.setattr(recorder, "_after_run", AsyncMock())
        await recorder.start()
        for event in events:
            await recorder.record(event)
        await recorder.finish(output_text="done", result_event=events[-1])

    asyncio.run(record())
    return {
        "commands": commands,
        "records": {name: [(call.args, call.kwargs) for call in mock.await_args_list] for name, mock in saved.items()},
    }


def test_disabled_signals_match_main_bytes(monkeypatch):
    assert_default_prompts_and_tools()
    baseline = json.loads((Path(__file__).parent / "fixtures/limit_signals/default_off.json").read_text())
    got = captured(monkeypatch)
    _frozen(got["commands"], baseline["commands"])
    _frozen(got["records"], baseline["records"])
