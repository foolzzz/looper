"""Recovery runs the actual private processor path, with one record and directory per attempt."""
from __future__ import annotations

import asyncio
import json

import pytest

from looper import child_env
from looper.agents import failure_handling, failures, model_call, processor
from looper.agents.agent_settings import AgentType
from looper.db import system_settings
from tests.db_support import DSN, run_db
from tests.fake_claude import fake_claude

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")

SCRIPT = r'''
import json, os, pathlib, sys
turn = json.loads(sys.stdin.readline())["message"]["content"]
counter = pathlib.Path(os.environ["RECOVERY_ATTEMPTS_FILE"])
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
def emit(value):
    print(json.dumps(value), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "recovery-" + str(n), "tools": [], "mcp_servers": []})
emit({"type": "assistant", "message": {"role": "assistant", "model": "claude-opus", "id": str(n),
      "content": [{"type": "thinking", "thinking": turn}], "usage": {"input_tokens": 10, "output_tokens": 2}}})
emit({"type": "result", "subtype": "error_during_execution" if n < 3 else "success", "is_error": n < 3,
      "api_error": "connection_failed" if n < 3 else None, "total_cost_usd": 0.1,
      "session_id": "recovery-" + str(n), "result": turn if n < 3 else "A short summary",
      "usage": {"input_tokens": 10, "output_tokens": 2}})
'''


def test_processor_actual_attempts_accumulate_cost_without_recording_batch(monkeypatch, tmp_path):
    monkeypatch.setenv("RECOVERY_ATTEMPTS_FILE", str(tmp_path / "attempts"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"RECOVERY_ATTEMPTS_FILE"})
    monkeypatch.setattr(processor.random, "uniform", lambda low, high: 1.0)

    async def test(pool):
        await system_settings.put_settings({"engine.failure_typing": "act"}, pool=pool)
        service = failure_handling.FailureHandlingService(pool=pool, mode=failures.FailureTyping.ACT)
        waits = []

        async def wait(delay, **kwargs):
            if delay in (120, 600):
                waits.append(delay)
                return True
            await asyncio.Future()  # admission's cancellation watcher, cancelled on grant

        monkeypatch.setattr(service, "wait_turn", wait)
        monkeypatch.setattr(failure_handling, "_service", service)
        request = model_call.ProcessorCall(
            owner="synthetic-recovery", instructions="Summarize.", input_text="private-token-8812",
            input_ref={"kind": "synthetic", "from_seq": 1, "to_seq": 3}, quiet_word="NONE",
            budget_usd=1.0, timeout_s=2000, runtime_root=tmp_path / "runtime",
            claude_bin=fake_claude(tmp_path / "claude", SCRIPT), role=AgentType.PROCESSOR)
        done = model_call.processor_result(await model_call.call(request, parse=str))
        assert done.reply == "A short summary" and done.cost_usd == pytest.approx(0.3)
        assert (tmp_path / "attempts").read_text() == "3" and waits == [120, 600]
        rows = await pool.fetch("SELECT id,input_text,output_text,error,metadata,failure_class,failure_cause "
                                "FROM llm_records WHERE agent_id='processor:synthetic-recovery' ORDER BY id")
        assert len(rows) == 3 and done.record_id == rows[-1]["id"]
        assert [(row["failure_class"], row["failure_cause"]) for row in rows] == [
            ("transient", "connection_failed"), ("transient", "connection_failed"), (None, None)]
        events = await pool.fetch("SELECT e.event FROM llm_record_events e JOIN llm_records r ON r.id=e.record_id "
                                  "WHERE r.agent_id='processor:synthetic-recovery'")
        assert "private-token-8812" not in json.dumps([dict(row) for row in rows], default=str)
        assert "private-token-8812" not in json.dumps([dict(row) for row in events], default=str)
        assert not list((tmp_path / "runtime" / ".local" / "processor").iterdir())

    run_db(test, monkeypatch)
