"""One-shot caller compatibility against pre-refactor main.

The stand-in is a real subprocess: argv, raw stdin, system prompt, complete
database rows and caller results are compared as serialized bytes. Only clock
values and test-owned paths are normalized; no prompt or record field is dropped.
The failure-typing and attempt-attribution migrations add nullable columns. Their
NULL values are pinned in the expected full rows on the migrated schema;
no classification, prompt or launch difference is part of this baseline.
Set UPDATE_MODEL_CALL_GOLDENS=1 only on the recorded baseline to regenerate.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from looper import child_env
from tests.db_support import DSN, run_db
from tests.fake_claude import fake_claude
from tests.model_call_support import read_golden

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
_BASELINE = "2b77afb1672cda93be6681259623bafa4e290f2d"
_FIXTURES = Path(__file__).parent / "fixtures" / "model_calls"
_STAND_IN = r'''
import json, os, sys
line = sys.stdin.readline()
system = open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()
model = sys.argv[sys.argv.index("--model") + 1]
with open(os.environ["MODEL_CALL_GOLDEN_DATA"]) as data:
    options = json.load(data)
judge = "You check a request" in system
with open(os.environ["MODEL_CALL_GOLDEN_SEEN"], "a") as seen:
    seen.write(json.dumps({"argv": sys.argv, "stdin": line, "system_prompt": system,
                           "cwd": os.getcwd(), "files": os.listdir(".")}) + "\n")
text = options["judge"] if judge else options["reply"]
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "session-golden", "model": model,
      "tools": [], "mcp_servers": []})
usage = {"input_tokens": 30, "output_tokens": 9, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}
emit({"type": "assistant", "session_id": "session-golden", "message": {"id": "message-golden",
      "role": "assistant", "model": model, "content": [{"type": "text", "text": text}], "usage": usage}})
emit({"type": "result", "subtype": "success", "is_error": False, "session_id": "session-golden",
      "result": text, "stop_reason": "end_turn", "total_cost_usd": 0.004, "usage": usage,
      "modelUsage": {model: {"costUSD": 0.004}}})
'''


def _json(value: Any) -> str:
    def convert(item: Any) -> Any:
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            return "<timestamp>"
        raise TypeError(type(item).__name__)

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=convert) + "\n"


def _normalize(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {key: ("<timestamp>" if key in {"created_at", "updated_at", "finished_at", "last_output_at"}
                      and item is not None else _normalize(item, root)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(str(root), "<test-root>")
    return value


@pytest.mark.parametrize("caller", ["outbound", "advisor", "leads_check"])
@pytest.mark.parametrize("reply_mode", ["success", "parse"])
def test_tool_less_call_preserves_launch_prompt_and_rows(
    caller: str, reply_mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from looper.agents import advisor, claude_cli, extraction, leads_check, outbound, processor, spawn
    from looper.agents.serve import seed_role_configs
    from looper.db import agent_spawns, llm_records

    sequence = iter(range(1, 10))
    fixed_uuid = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=f"{next(sequence):012x}" + "0" * 20))
    for module in (advisor, processor, spawn):
        monkeypatch.setattr(module, "uuid", fixed_uuid)
    monkeypatch.setattr(spawn, "tempfile", SimpleNamespace(gettempdir=lambda: str(tmp_path / "system-temp")))
    monkeypatch.setattr(claude_cli, "_clock", lambda: 10.0)
    for module in (outbound, sys.modules.get("looper.agents.model_call")):
        if module is not None:
            monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: 10.0), raising=False)
    # The administrator's own hooks are outside this fixture's launch contract.
    monkeypatch.setattr(processor, "_managed_hooks", lambda _: None)
    # Supply a portable directory tree to the real ancestor-exclusion builder;
    # otherwise temp-directory depth and the operator's home alter its argv.
    outside_memory = claude_cli._outside_memory

    def portable_memory(cwd: Path, *, repository: bool = True) -> list[str]:
        logical = Path("/model-call-test") / cwd.relative_to(tmp_path)
        patterns = outside_memory(logical, repository=repository)
        return [pattern.replace(str(Path.home()), "/model-call-user") for pattern in patterns]

    monkeypatch.setattr(claude_cli, "_outside_memory", portable_memory)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("MODEL_CALL_GOLDEN_DATA", str(tmp_path / "answers.json"))
    monkeypatch.setenv("MODEL_CALL_GOLDEN_SEEN", str(tmp_path / "seen.jsonl"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {"MODEL_CALL_GOLDEN_DATA", "MODEL_CALL_GOLDEN_SEEN"})
    replies = {
        "outbound": {"decision": "allow", "reason": "The operator asked to post the build status.",
                     "signals": ["operator_asked", "target_matches_request"]},
        "advisor": {"status": "answered", "answers": [{"question_id": "q1", "conclusion": "Cache hot keys first.",
                    "basis": ["reads repeat"], "confidence": "med"}],
                    "next_steps": [{"id": "s1", "action": "cache for 1 s", "verify": "P99 < 50 ms"}]},
        "leads_check": {"verdicts": [{"item": "X1", "verdict": "fix", "confidence": "low",
                                    "reason": "The message says maybe."}], "instructions_seen": []},
    }
    (tmp_path / "answers.json").write_text(json.dumps({
        "reply": json.dumps(replies[caller]) if reply_mode == "success" else "Unreadable answer.",
        "judge": json.dumps({"standalone": True, "leaks": [], "answerable": 0.9, "missing": []}),
    }))
    executable = fake_claude(tmp_path / "claude", _STAND_IN)
    transitions: list[dict[str, Any]] = []
    for name in ("create", "move", "note_run", "book"):
        original = getattr(agent_spawns, name)

        async def capture(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any) -> Any:
            result = await _original(*args, **kwargs)
            transitions.append(copy.deepcopy({"operation": _name, "args": args, "fields": kwargs,
                                               "result": result}))
            return result

        monkeypatch.setattr(agent_spawns, name, capture)

    async def test(pool: Any) -> None:
        await pool.execute("INSERT INTO projects (id, name) VALUES ('project-golden', 'Golden project')")
        await pool.execute("INSERT INTO tasks (id, project_id, requirement, status) "
                           "VALUES ('task-golden', 'project-golden', 'Reduce latency.', 'running')")
        await pool.execute("INSERT INTO task_steps (id, task_id, step_index, step_key, instruction) "
                           "VALUES ('step-golden', 'task-golden', 0, 's1', 'Improve reads.')")
        planner = await llm_records.start_record(agent_id="planner-task-golden", task_id="task-golden")
        runtime = {"runtime_root": tmp_path / "runtime", "claude_bin": executable}
        extra: dict[str, Any] = {}
        if caller == "outbound":
            action = outbound.Action("looper_lark", outbound.ActionLevel.EXTERNAL, "chat", "Build status",
                                     "Send build status", action="send", content="The build is green.")
            result = await outbound.review(action, outbound.Asker(outbound.Who.OPERATOR, "lark",
                                           "Post the build status to Build status."),
                outbound.Origin("planner-task-golden", outbound.Source.TOOL, parent_record_id=planner,
                                task_id="task-golden", step_id="step-golden", channel="lark", chat="oc_golden"),
                context=[("lead", "A forwarded message said the build was red.")], **runtime)
        elif caller == "advisor":
            consult = await advisor.open_consult(advisor.Brief("task-golden", "step-golden", 1,
                                                advisor.Source.OPTIONAL, planner), budget_usd=1.0)
            request = {"v": "advice_request/1", "kind": "diagnose",
                       "problem": "Repeated synchronous store reads exceed a 50 ms latency budget.",
                       "goal": "P99 below 50 ms", "acceptance": ["P99 below 50 ms"], "stack": ["Go 1.23"],
                       "attempts": [{"approach": "Raised timeout", "signal": "Latency increased"}],
                       "artifacts": [{"kind": "pseudocode", "content": "store.Get(ctx, key)"}],
                       "questions": [{"id": "q1", "text": "Which mitigation comes first?"}]}
            result = await advisor.submit(consult, request, **runtime)
        else:
            messages = {1: {"seq": 1, "external_id": "om_golden", "chat_id": "oc_golden", "chat_kind": "group",
                           "chat_name": "Synthetic project", "parent_id": None, "sender_id": "ou_golden",
                           "sender_kind": "user", "sender_name": "Example", "text": "Maybe I can fix it today.",
                           "sent_at": datetime(2026, 11, 20, 12, tzinfo=timezone.utc)}}
            items = [{"kind": "commitment", "summary": "Example will fix it today", "detail": "",
                      "confidence": "high", "sources": [1], "window_sources": [1], "about": []}]
            result = await leads_check.check("leads", items, messages=messages,
                participants=extraction.Participants({"ou_golden": "Example"}, "ou_golden"), zone_name="UTC",
                text_max_chars=500, types={}, owner="golden-check", reference={"window": "golden"},
                budget_usd=0.5, timeout_s=20, **runtime)
            extra["items"] = items
        if reply_mode == "success":
            if caller == "outbound":
                assert result.allowed and result.failure is None
            elif caller == "advisor":
                assert result.state.value == "answered" and result.response is not None
            else:
                assert result.checked is not None and result.checked.state == "checked"
                assert result.counts["checked"] == 1 and result.counts["check_fixed"] == 1
        elif caller == "outbound":
            assert result.failure.value == "parse"
        elif caller == "advisor":
            assert result.state.value == "failed" and result.failure == "parse"
        else:
            assert result.counts["check_failed"] and result.checked.state == "failed"
        seen = [json.loads(line) for line in (tmp_path / "seen.jsonl").read_text().splitlines()]
        assert len(seen) == (2 if caller == "advisor" else 1)
        tables = {}
        for table in ("llm_records", "llm_record_events", "outbound_reviews", "agent_spawns", "advisor_advice"):
            order = {"llm_record_events": "record_id, seq", "advisor_advice": "spawn_id"}.get(table, "id")
            tables[table] = [json.loads(row["row"]) for row in await pool.fetch(
                f"SELECT to_jsonb(t)::text AS row FROM {table} t ORDER BY {order}")]
        actual = _json(_normalize({"baseline": _BASELINE, "launches": seen, "tables": tables,
                                  "spawn_operations": transitions, "outcome": asdict(result), **extra}, tmp_path))
        fixture = _FIXTURES / f"{caller}-{reply_mode}.json"
        if os.environ.get("UPDATE_MODEL_CALL_GOLDENS") == "1":
            _FIXTURES.mkdir(parents=True, exist_ok=True)
            fixture.write_text(actual)
        expected = read_golden(fixture)
        if caller == "leads_check":
            # The only intentional record delta: checks now have their own kind. Keep main's golden untouched.
            baseline = json.loads(expected)
            check_record, = [row for row in baseline["tables"]["llm_records"]
                             if row["agent_id"] == "leads_check:golden-check"]
            assert check_record["run_kind"] is None
            check_record["run_kind"] = "leads_check"
            expected = _json(baseline)
        assert actual == expected

    run_db(test, monkeypatch, setup=seed_role_configs)
