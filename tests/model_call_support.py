"""Portable subprocess and row goldens for the remaining tool-less callers.

Regenerate only with production sources from BASELINE. Clock values and
test-owned paths are normalized; prompts and record fields are retained.
"""

from __future__ import annotations

import json
import os
import subprocess
import difflib
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.fake_claude import fake_claude

BASELINE = "1f2ac843e01adac83448c09a329df01a4dbb8566"
_FIXTURES = Path(__file__).parent / "fixtures" / "model_calls"
_STAND_IN = r'''
import json, os, sys, time
line = sys.stdin.readline()
system = open(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read()
model = sys.argv[sys.argv.index("--model") + 1]
model = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-4-5"}.get(model, model)
seen_path = os.environ["MODEL_CALL_GOLDEN_SEEN"]
count = len(open(seen_path).readlines()) if os.path.exists(seen_path) else 0
with open(os.environ["MODEL_CALL_GOLDEN_OPTIONS"]) as data:
    options = json.load(data)[count]
with open(seen_path, "a") as seen:
    seen.write(json.dumps({"argv": sys.argv, "stdin": line, "system_prompt": system,
                           "cwd": os.getcwd(), "files": os.listdir(".")}) + "\n")
mode, text = options.get("mode", "success"), options.get("reply", "")
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "session-golden", "model": model,
      "tools": [], "mcp_servers": []})
if mode == "timeout":
    time.sleep(60)
if mode == "no_result":
    sys.exit(0)
if mode == "blocked":
    reason = "Request Blocked by Security Policy"
    emit({"type": "system", "subtype": "informational", "session_id": "session-golden",
          "content": "UserPromptSubmit operation blocked by hook:\n" + reason})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0,
          "session_id": "session-golden", "result": reason + "\nOriginal prompt: " + line,
          "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})
    sys.exit(0)
usage = {"input_tokens": 30, "output_tokens": 9, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0}
emit({"type": "assistant", "session_id": "session-golden", "message": {"id": "message-golden",
      "role": "assistant", "model": model, "content": [{"type": "text", "text": text}], "usage": usage}})
failed = mode in ("runtime", "rate_limited")
cost = 0.5 if mode == "budget" else 0.004
emit({"type": "result", "subtype": "error_during_execution" if failed else "success", "is_error": failed,
      "session_id": "session-golden", "result": text,
      "stop_reason": "refusal" if mode == "refused" else "end_turn", "total_cost_usd": cost,
      "usage": usage, "modelUsage": {model: {"costUSD": cost}},
      **({"api_error_status": 429} if mode == "rate_limited" else {}),
      **({"errors": ["synthetic runtime failure"]} if mode == "runtime" else {})})
'''


def golden_cli(monkeypatch: Any, root: Path, *, runs: list[dict[str, Any]],
               uuid_modules: tuple[Any, ...] = ()) -> str:
    """Capture the actual CLI launch while keeping its test-owned environment portable."""
    from looper import child_env
    from looper.agents import claude_cli, model_call, processor, spawn

    sequence = iter(range(1, 100))
    fixed_uuid = SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=f"{next(sequence):012x}" + "0" * 20))
    for module in (processor, spawn, *uuid_modules):
        monkeypatch.setattr(module, "uuid", fixed_uuid)
    monkeypatch.setattr(spawn, "tempfile", SimpleNamespace(gettempdir=lambda: str(root / "system-temp")))
    monkeypatch.setattr(claude_cli, "_clock", lambda: 10.0)
    monkeypatch.setattr(model_call, "time", SimpleNamespace(monotonic=lambda: 10.0))
    monkeypatch.setattr(processor, "_managed_hooks", lambda _: None)
    outside_memory = claude_cli._outside_memory

    def portable_memory(cwd: Path, *, repository: bool = True) -> list[str]:
        logical = Path("/model-call-test") / cwd.relative_to(root)
        return [pattern.replace(str(Path.home()), "/model-call-user")
                for pattern in outside_memory(logical, repository=repository)]

    monkeypatch.setattr(claude_cli, "_outside_memory", portable_memory)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (root / "answers.json").write_text(json.dumps(runs))
    monkeypatch.setenv("MODEL_CALL_GOLDEN_OPTIONS", str(root / "answers.json"))
    monkeypatch.setenv("MODEL_CALL_GOLDEN_SEEN", str(root / "seen.jsonl"))
    monkeypatch.setattr(child_env, "_allowed", child_env._allowed | {
        "MODEL_CALL_GOLDEN_OPTIONS", "MODEL_CALL_GOLDEN_SEEN"})
    return fake_claude(root / "claude", _STAND_IN)


def read_launches(root: Path) -> list[dict[str, Any]]:
    path = root / "seen.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def table_rows(pool: Any, tables: tuple[str, ...]) -> dict[str, Any]:
    """Read every column, including JSON columns, through PostgreSQL's serializer."""
    rows = {}
    for table in tables:
        order = {"llm_record_events": "record_id, seq", "advisor_advice": "spawn_id",
                 "subscriptions": "schedule_id", "subscription_runs": "run_id",
                 "subscription_deliveries": "run_id, segment",
                 "message_lead_sources": "lead_id, event_seq",
                 "message_lead_people": "lead_id, open_id, role"}.get(table, "id")
        rows[table] = [json.loads(row["row"]) for row in await pool.fetch(
            f"SELECT to_jsonb(t)::text AS row FROM {table} t ORDER BY {order}")]
    return rows


def _normalize(value: Any, root: Path) -> Any:
    if isinstance(value, dict):
        return {key: ("<timestamp>" if key in {"created_at", "updated_at", "finished_at", "last_output_at",
                                             "cursor_at", "enabled_at"}
                      and item is not None else _normalize(item, root)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(str(root), "<test-root>")
    return value


def assert_golden(name: str, payload: Any, root: Path) -> None:
    def convert(item: Any) -> Any:
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, datetime):
            return "<timestamp>"
        raise TypeError(type(item).__name__)

    actual = json.dumps(_normalize({"baseline": BASELINE, **payload}, root), ensure_ascii=False,
                        indent=2, sort_keys=True, default=convert) + "\n"
    fixture = _FIXTURES / f"{name}.json"
    if os.environ.get("UPDATE_MODEL_CALL_GOLDENS") == "1":
        import looper

        repository = Path(__file__).resolve().parents[1]
        assert Path(looper.__file__).resolve().parent == repository / "looper", "load baseline checkout's package"
        unchanged = subprocess.run(["git", "diff", "--quiet", BASELINE, "--", "looper"], cwd=repository)
        assert unchanged.returncode == 0, "regenerate goldens only with production sources from baseline main"
        _FIXTURES.mkdir(parents=True, exist_ok=True)
        fixture.write_text(actual)
    expected = fixture.read_text()
    assert actual == expected, "\n".join(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                                           fromfile="main", tofile="caller"))
