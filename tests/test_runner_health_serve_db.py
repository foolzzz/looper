"""Real 10s/30s health reclaim through an isolated serve and a one-shot database.

The peer finishes one platform read, accepts a second turn, then stops answering
only ``runner/health`` while the process stays up. No new switch is involved.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import unquote, urlsplit

import pytest

from evals.prompt_regression.runner import EvalError
from looper.agents.runtimes import ADAPTERS
from looper.agents.runtimes.runner.supervisor import (
    HEALTH_INTERVAL_SECONDS, HEALTH_TIMEOUT_SECONDS)
from tests.db_support import fresh_database_without_recording
from tests.runner_support import require_disposable_database, serving
from tests.test_runner_serve_db import FACT, ROOT, events, query, seed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _lines(log: Path, prefix: str) -> list[dict]:
    rows = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            rows.append(json.loads(line.removeprefix(prefix)))
    return rows


def _wire(log: Path) -> list[dict]:
    marker = "runner fake stderr: "
    found = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if marker in line and "{" in line:
            found.append(json.loads(line.split(marker, 1)[1]))
    return found


def _send(api, message_id: str) -> None:
    api.post("/api/v1/operator_chat/send", {"text": "List the tasks.",
        "session_id": "runner-acceptance", "message_id": message_id})


def _reply(api, message_id: str) -> str | None:
    messages = api.post("/api/v1/operator_chat/messages",
                        {"session_id": "runner-acceptance"})["result"]["messages"]
    for message in messages:
        if message["message_id"] == message_id + ":reply":
            return message["content"]
    return None


def _wait_reply(api, message_id: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        content = _reply(api, message_id)
        if content is not None:
            return content
        time.sleep(0.2)
    pytest.fail(f"{message_id} produced no reply within {timeout:.0f}s")


def _lost_record(url: str):
    for record in query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id"):
        results = [event for event in events(url, record) if event.get("type") == "result"]
        if results and results[-1].get("runner", {}).get("outcome") == "lost":
            return record, results[-1]
    return None, None


def test_isolated_serve_recovers_unresponsive_runner_without_replaying_turn(tmp_path, monkeypatch):
    import looper

    assert HEALTH_INTERVAL_SECONDS == 10 and HEALTH_TIMEOUT_SECONDS == 30
    assert "test-runner" not in ADAPTERS
    assert Path(looper.__file__).resolve() == ROOT / "looper/__init__.py"
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("serve acceptance untested: disposable LOOPER_TEST_DATABASE_URL required")
    parts = urlsplit(admin)
    name = unquote(parts.path.lstrip("/"))
    assert parts.hostname in {"127.0.0.1", "localhost"}
    assert name and name not in {"looper", "loopx"}
    # The password stays in the environment the child already inherits. A failure
    # must not interpolate the URL.
    monkeypatch.setenv("PYTEST_LOOPER_DISPOSABLE_PG_PASSWORD", parts.password or "")
    monkeypatch.setenv("PYTEST_LOOPER_SESSION_CAP", "1")
    monkeypatch.setenv("PYTEST_LOOPER_HOLD_SIBLING", "1")
    require_disposable_database(admin, served=False)
    log = tmp_path / "serve.log"
    with ExitStack() as resources:
        try:
            url, _offset = resources.enter_context(fresh_database_without_recording(admin))
        except EvalError:
            pytest.skip("serve acceptance untested: disposable PostgreSQL is unavailable or lacks CREATEDB")
        served = urlsplit(url)
        assert unquote(served.path.lstrip("/")).startswith("looper_eval_")
        assert served.hostname == parts.hostname and served.port == parts.port
        with serving(database_url=url, runtime_root=tmp_path / "runtime", home=tmp_path / "home",
                     work_dir=tmp_path / "work", log_path=log) as api:
            assert ":8767" not in api.base_url
            profile = seed(url, api)
            assert not any(row["enabled"] for row in query(url, "SELECT enabled FROM channel_configs"))
            api.post("/api/v1/agent_settings/update_role", {"role": "manager", "values": {
                "profile_id": profile, "run_budget_usd": 1}})
            _send(api, "health-first")
            first_reply = _wait_reply(api, "health-first", timeout=25)
            assert FACT in first_reply and "Platform result" in first_reply
            (first,) = query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")
            assert first["status"] == "completed"
            first_events = events(url, first)
            (first_result,) = [event for event in first_events if event["type"] == "result"]
            assert first_result["runner"]["outcome"] == "completed"
            first_trace = first_result["trace_id"]
            (call,) = [block for event in first_events if event["type"] == "assistant"
                       for block in event["message"]["content"] if block["type"] == "tool_use"]
            assert (call["name"], call["input"]) == ("looper_tasks", {"action": "list"})

            text = log.read_text(encoding="utf-8", errors="replace")
            assert sys.executable in text and str(ROOT / "looper/__init__.py") in text
            sibling = _lines(log, "runner test sibling=")
            runners = _lines(log, "runner test runner=")
            assert len(sibling) == 1 and len(runners) == 2
            sibling_pid = sibling[0]["pid"]
            (manager_pid,) = [row["pid"] for row in runners if row["pid"] != sibling_pid]
            assert _alive(manager_pid) and _alive(sibling_pid)
            prompted = list((tmp_path / "runtime" / "runners").glob("fake-*/prompted"))
            assert len(prompted) == 1
            member = prompted[0].parent
            (member / "hold-turn").write_text("1", encoding="utf-8")

            _send(api, "health-hung")
            deadline = time.monotonic() + 40
            ended = []
            while time.monotonic() < deadline:
                ended = [event for event in _wire(log) if event.get("event") == "request_end"
                         and event.get("trace_id") not in {None, first_trace}]
                if ended:
                    break
                time.sleep(0.1)
            assert ended, "the second turn did not record usage before health was withheld"
            assert tuple(ended[0][key] for key in ("input_uncached", "output", "cache_read", "cache_write")) == (
                1000, 200, 300, 400)
            assert _alive(manager_pid)
            hung_at = time.monotonic()
            (member / "hang-health").write_text("1", encoding="utf-8")

            lost, lost_result = None, None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                lost, lost_result = _lost_record(url)
                if lost is not None and _reply(api, "health-hung") is not None:
                    break
                time.sleep(0.5)
            assert lost is not None and lost_result is not None, "hung turn was not settled lost"
            elapsed = time.monotonic() - hung_at
            assert elapsed >= 25, f"health reclaim returned in {elapsed:.1f}s, before the real deadline"
            lost_reply = _reply(api, "health-hung")
            assert lost_reply is not None
            assert FACT not in lost_reply and "Platform result" not in lost_reply
            assert "did not succeed" in lost_reply
            assert lost["status"] != "completed"
            assert lost["cost_usd"] is None
            assert tuple(lost[key] for key in (
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (
                1000, 200, 300, 400)
            lost_events = events(url, lost)
            lost_results = [event for event in lost_events if event["type"] == "result"]
            assert lost_results == [lost_result]
            assert lost_result["usage_complete"] is False
            assert lost_result["usage"] == {"input_tokens": 1000, "output_tokens": 200,
                "cache_read_input_tokens": 300, "cache_creation_input_tokens": 400}
            assert lost_result["total_cost_usd"] is None
            assert lost_result["trace_id"] != first_trace
            lost_calls = [block for event in lost_events if event["type"] == "assistant"
                          for block in event["message"]["content"] if block["type"] == "tool_use"]
            assert len(lost_calls) == 1 and lost_calls[0]["name"] == "looper_tasks"
            diagnostics = log.read_text(encoding="utf-8", errors="replace")
            assert "runner_health_failed" in diagnostics and "reason=timeout" in diagnostics
            reap_deadline = time.monotonic() + 10
            while _alive(manager_pid) and time.monotonic() < reap_deadline:
                time.sleep(0.1)
            assert not _alive(manager_pid)
            assert _alive(sibling_pid)
            api.post("/api/v1/system/status", {})

            frozen_events = events(url, lost)
            frozen_actions = query(url, "SELECT id, status, tool, arguments FROM manager_actions ORDER BY id")
            _send(api, "health-next")
            next_reply = _wait_reply(api, "health-next", timeout=45)
            assert FACT in next_reply and "Platform result" in next_reply
            assert events(url, lost) == frozen_events
            assert not _alive(manager_pid) and _alive(sibling_pid)
            recovered = [row for row in query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")
                         if row["id"] != first["id"] and row["id"] != lost["id"] and row["status"] == "completed"]
            assert recovered, "the following message did not complete on a replacement"
            recovered_events = events(url, recovered[-1])
            (recovered_result,) = [event for event in recovered_events if event["type"] == "result"]
            assert recovered_result["runner"]["outcome"] == "completed"
            assert recovered_result["trace_id"] not in {first_trace, lost_result["trace_id"]}
            runners = _lines(log, "runner test runner=")
            new_pids = [row["pid"] for row in runners if row["pid"] not in {manager_pid, sibling_pid}]
            assert new_pids and _alive(new_pids[-1])
            actions = query(url, "SELECT id, status, tool, arguments FROM manager_actions ORDER BY id")
            assert actions[:len(frozen_actions)] == frozen_actions
            new = actions[len(frozen_actions):]
            # The next message records its own send plus one platform read.
            # The lost trace's tool row stays in the frozen prefix.
            tool_rows = [row for row in new if row["tool"] == "looper_tasks"]
            assert len(tool_rows) == 1
            assert json.loads(tool_rows[0]["arguments"]) == {"action": "list"}
            sends = [row for row in new if row["tool"] != "looper_tasks"]
            assert len(sends) == 1 and "health-next" in sends[0]["arguments"]
            api.post("/api/v1/system/status", {})
    assert "test-runner" not in ADAPTERS
