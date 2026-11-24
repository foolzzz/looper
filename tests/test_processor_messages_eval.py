"""The eval's direct Messages path, against a scripted local HTTP endpoint only."""

import asyncio
import json
import threading
import time
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from evals.processor import __main__ as evaluate
from looper.agents import extraction, processor
from looper.agents.agent_settings import AgentType, resolve
from looper.db import connection
from tests.db_support import DSN

KEY = "synthetic-secret-do-not-output"


@pytest.fixture
def endpoint():
    state = Namespace(requests=[], status=200, delay=0, reply="NONE", stop="end_turn", blocks=None, escape_key=False, usage={
        "input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 30})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state.requests.append((self.path, dict(self.headers), json.loads(self.rfile.read(
                int(self.headers["Content-Length"])))))
            time.sleep(state.delay)
            body = json.dumps({"type": "message", "model": "deepseek-flash", "stop_reason": state.stop,
                               "content": state.blocks or [{"type": "text", "text": state.reply}], "usage": state.usage}
                              if state.status == 200 else {"error": {"message": KEY}}).encode()
            if state.escape_key:
                body = body.replace(KEY.encode(), KEY.replace("s", r"\u0073").encode())
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    assert server.server_port != 8767
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def args(monkeypatch, endpoint):
    from evals.processor.messages import MessagesEngine

    monkeypatch.setenv("EVAL_TEST_KEY", KEY)
    monkeypatch.setattr(processor, "_role", AsyncMock(return_value=processor._Role(
        processor.PROCESSOR_ROLE_PROMPT.strip(), True, 0, 0, True)))
    monkeypatch.setattr(connection, "get_pool", lambda: None)
    monkeypatch.setattr(extraction, "intent_types", AsyncMock(return_value=[
        {"key": "bug_triage", "name": "Bug", "guidance": "Find bugs"}]))
    return Namespace(budget_usd=1, timeout=1, claude="unused", untrusted_fence=False,
                     messages_engine=MessagesEngine(endpoint.url, "deepseek-flash", "EVAL_TEST_KEY"))


@pytest.mark.parametrize("fence", [False, True])
def test_request_reuses_processor_bytes_and_prices_usage(args, endpoint, tmp_path, fence):
    args.messages_engine.fence = fence
    run = asyncio.run(evaluate._extract(args, "intents_group_requests_quiet", 1, tmp_path))
    assert run["passed"], run
    path, headers, body = endpoint.requests[0]
    assert path == "/v1/messages" and headers["x-api-key"] == KEY
    assert "tools" not in body and "tool_choice" not in body
    case = evaluate.EXTRACTION_CASES["intents_group_requests_quiet"]
    types = asyncio.run(extraction.intent_types(None))
    text, _ = extraction.render("intents", case["rows"], context=[],
        participants=extraction.Participants(evaluate._NAMES, evaluate._USER), known=[],
        zone_name="Asia/Shanghai", text_max_chars=500)
    prompt = processor._system_prompt(resolve(AgentType.PROCESSOR, {}),
        asyncio.run(processor._role()), extraction.instructions("intents", extraction.INTENTS_INSTRUCTIONS,
        types, group=True), evaluate.EXTRACTION_QUIET, extraction.INTENTS_CONTRACT)
    assert body["system"].encode() == prompt.text.encode()
    assert body["messages"] == [{"role": "user", "content": processor.user_turn(text, fence=fence)}]
    assert run["usage"] == endpoint.usage
    assert run["cost_usd"] == pytest.approx((100 * .3 + 10 * 1.2 + 20 * .006 + 30 * .3) / 1e6)
    assert KEY not in json.dumps(run)


@pytest.mark.parametrize("status,delay,failure", [(401, 0, "HTTP 401"), (429, 0, "HTTP 429"),
    (503, 0, "HTTP 503"), (200, .8, "timeout")])
def test_http_failures_fail_case_and_halt_unknown_spend(args, endpoint, tmp_path, capsys, status, delay, failure):
    endpoint.status, endpoint.delay = status, delay
    args.timeout = .4 if delay else 1
    run = asyncio.run(evaluate._extract(args, "intents_group_requests_quiet", 1, tmp_path))
    assert not run["passed"] and run["failure"] and failure in run["detail"]
    assert run["cost_usd"] is None and args.messages_engine.halted
    again = asyncio.run(evaluate._extract(args, "intents_group_requests_quiet", 2, tmp_path))
    assert not again["passed"] and len(endpoint.requests) == 1
    assert KEY not in capsys.readouterr().out + json.dumps([run, again])


def test_budget_refuses_before_request(args, endpoint, tmp_path):
    args.budget_usd = 0.000001
    run = asyncio.run(evaluate._extract(args, "intents_group_requests_quiet", 1, tmp_path))
    assert not run["passed"] and run["failure"] == "budget" and run["cost_usd"] == 0
    assert not endpoint.requests


@pytest.mark.parametrize("reply,usage,escape", [(KEY, {"input_tokens": 1, "output_tokens": 1}, False),
    (KEY, {"input_tokens": 1, "output_tokens": 1}, True), ("NONE", {}, False)])
def test_echoed_key_or_missing_usage_cannot_pass(args, endpoint, tmp_path, reply, usage, escape):
    endpoint.reply, endpoint.usage, endpoint.escape_key = reply, usage, escape
    run = asyncio.run(evaluate._extract(args, "intents_group_requests_quiet", 1, tmp_path))
    assert not run["passed"] and KEY not in json.dumps(run)


@pytest.mark.parametrize("stop,blocks", [("max_tokens", None), ("end_turn", [{"type": "tool_use",
    "id": "synthetic-tool", "name": "never_execute", "input": {}}])])
def test_truncation_and_tool_calls_fail_with_usage_preserved(args, endpoint, tmp_path, stop, blocks):
    endpoint.stop, endpoint.blocks = stop, blocks
    run = asyncio.run(evaluate._extract(args, "intents_group_requests_quiet", 1, tmp_path))
    assert not run["passed"] and run["failure"] and run["cost_usd"] > 0
    assert run["usage"] == endpoint.usage and args.messages_engine.halted


@pytest.mark.parametrize("config", [{"model": "unpriced-model"}, {"base_url": "https://example.test/?secret=x"},
    {"api_key_env": "UNSET_SYNTHETIC_KEY"}, {"max_output_tokens": 0}])
def test_invalid_config_fails_before_network(monkeypatch, endpoint, config):
    from evals.processor.messages import MessagesEngine

    monkeypatch.setenv("EVAL_TEST_KEY", KEY)
    monkeypatch.delenv("UNSET_SYNTHETIC_KEY", raising=False)
    with pytest.raises(ValueError):
        MessagesEngine(**{"base_url": endpoint.url, "model": "deepseek-flash", "api_key_env": "EVAL_TEST_KEY", **config})
    assert not endpoint.requests


def test_labeled_windows_share_case_budget_and_keep_only_counts(args, endpoint, tmp_path):
    window = {"type": "window", "messages": [{"ref": "x1", "sender": "P1",
        "sent_at": "2026-11-20T01:00:00+00:00", "text": "Private synthetic text"}]}
    path = tmp_path / "set.jsonl"
    path.write_text(json.dumps(window) + '\n' + json.dumps({"type": "label", "expected": "none", "sources": ["x1"]}))
    windows, labels = evaluate._intents_file_cases(path)
    run = asyncio.run(evaluate._intents_file(args, windows, labels, 1, tmp_path))
    assert run["passed"] and run["windows"][0]["usage"] == endpoint.usage
    assert "Private synthetic text" not in json.dumps(run) and KEY not in json.dumps(run)
    args.budget_usd = .000001
    denied = asyncio.run(evaluate._intents_file(args, windows, labels, 2, tmp_path))
    assert not denied["passed"] and len(endpoint.requests) == 1


@pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")
@pytest.mark.parametrize("source", ["synthetic", "labeled"])
def test_cli_readback_and_budget_stop(monkeypatch, endpoint, tmp_path, capsys, source):
    monkeypatch.setenv("EVAL_TEST_KEY", KEY)
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"deepseek-flash": {"input_usd_per_mtok": 0, "output_usd_per_mtok": 1,
        "cache_read_usd_per_mtok": 0, "cache_write_usd_per_mtok": 0}}))
    out = tmp_path / "run.json"
    argv = ["processor", "--engine", "messages", "--base-url", endpoint.url, "--model", "deepseek-flash",
        "--api-key-env", "EVAL_TEST_KEY", "--pg-url", DSN, "--prices-file", str(prices),
        "--max-output-tokens", "10", "--case-budget-usd", ".001", "--total-budget-usd", ".000015",
        "--reps", "3", "--out", str(out)]
    if source == "synthetic":
        argv += ["--cases", "intents_group_requests_quiet"]
    else:
        path = tmp_path / "set.jsonl"
        path.write_text('\n'.join(json.dumps({"type": "window", "messages": [{"ref": f"x{i}", "sender": "P1",
            "sent_at": "2026-11-20T01:00:00+00:00", "text": "Private synthetic text"}]}) for i in (1, 2)))
        argv += ["--intents-file", str(path), "--case-budget-usd", ".000015"]
    monkeypatch.setattr("sys.argv", argv)
    assert evaluate.main() == 1  # incomplete evaluation must never pass
    result = json.loads(out.read_text())
    assert len(endpoint.requests) == 1
    assert sum(run["cost_usd"] or 0 for run in result["runs"]) == pytest.approx(.00001)
    assert any(not run["passed"] for run in result["runs"])
    assert result["engine"] == "messages" and result["model"] == "deepseek-flash"
    output = capsys.readouterr().out + out.read_text()
    assert KEY not in output and "Private synthetic text" not in output
