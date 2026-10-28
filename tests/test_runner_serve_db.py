"""Fake protocol peer through the real isolated Manager HTTP and accounting path."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import os
from decimal import Decimal
from pathlib import Path
import sys
import time
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from evals.prompt_regression.runner import EvalError, fresh_database
from tests.runner_support import (
    DISPOSABLE_SERVERS, OPERATOR_DATABASES, require_disposable_database, serving)

_CI_ADMIN = "postgresql://postgres:postgres@localhost:5432/postgres"
_LOCAL_ADMIN = "postgresql://postgres:postgres@127.0.0.1:54329/postgres"

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-runner-test-key"
FACT = "archived-orchid-6742"


def query(url, sql, *args, execute=False):
    async def run():
        conn = await asyncpg.connect(url, timeout=5, command_timeout=10)
        try:
            if execute:
                return await conn.execute(sql, *args)
            return [dict(row) for row in await conn.fetch(sql, *args)]
        finally:
            await conn.close()
    return asyncio.run(asyncio.wait_for(run(), 20))


def seed(url, api):
    # Only fixture rows, in the database just created by fresh_database.
    query(url, """
        INSERT INTO agent_runtimes (id,name,type,version,capabilities)
          VALUES ('rt-test-runner','Test runner','test-runner','0.1.0',
                  ARRAY['resident_session','resume']);
        INSERT INTO projects (id,name) VALUES ('proj-test-runner','Runner fixture');
        INSERT INTO tasks (id,project_id,requirement,status,phase)
          VALUES ('task-archived-orchid','proj-test-runner','archived-orchid-6742','completed','completed');
    """, execute=True)
    provider = api.post("/api/v1/provider_accounts/create", {
        "name": "Synthetic runner account", "vendor_type": "custom", "api_key": KEY,
        "endpoints": [{"protocol": "openai-responses", "base_url": "https://runner.example.invalid"}],
    })["result"]["provider"]
    model = api.post("/api/v1/ops/models/create", {"provider": provider["provider_id"],
        "name": "fake-model", "protocol": "openai-responses", "context_window": 100000,
        "input_usd_per_mtok": 1, "output_usd_per_mtok": 2,
        "cache_read_usd_per_mtok": 3, "cache_write_usd_per_mtok": 4})["result"]["model"]
    return api.post("/api/v1/profiles/create", {"name": "Synthetic runner",
        "runtime_id": "rt-test-runner", "model_id": model["model_id"]})["result"]["profile"]["profile_id"]


def ask(api, message_id):
    api.post("/api/v1/operator_chat/send", {"text": "List the tasks.",
        "session_id": "runner-acceptance", "message_id": message_id})
    deadline = time.monotonic() + 25
    envelope_id = None
    while time.monotonic() < deadline:
        # /turn is the existing live-turn read endpoint; /send submits the prompt.
        turn = api.post("/api/v1/operator_chat/turn", {"session_id": "runner-acceptance"})["result"]["turn"]
        if turn and turn["client_message_id"] == message_id:
            envelope_id = turn["message_id"]
        messages = api.post("/api/v1/operator_chat/messages",
                            {"session_id": "runner-acceptance"})["result"]["messages"]
        for message in messages:
            if message["message_id"] == message_id + ":reply":
                return message["content"], envelope_id
        time.sleep(0.1)
    pytest.fail("Manager did not answer within 25 seconds")


def events(url, record):
    return [json.loads(row["event"]) for row in query(url,
        "SELECT event FROM llm_record_events WHERE record_id=$1 ORDER BY seq", record["id"])]


@pytest.mark.parametrize("url", [
    _CI_ADMIN,
    "postgresql://postgres:postgres@localhost:5432/postgres_gw2",
    _LOCAL_ADMIN,
    "postgresql://postgres:postgres@localhost:54329/my_disposable",
    "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
])
def test_admin_url_accepts_only_the_disposable_servers(url):
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.port, parts.username) in DISPOSABLE_SERVERS
    assert unquote(parts.path.lstrip("/")) not in OPERATOR_DATABASES
    require_disposable_database(url, served=False)


@pytest.mark.parametrize("url", [
    "postgresql://postgres:postgres@localhost:5432/loopx",
    "postgresql://postgres:postgres@localhost:5432/looper",
    "postgresql://postgres:postgres@127.0.0.1:54329/loopx",
    "postgresql://postgres:postgres@127.0.0.1:54329/looper",
    "postgresql://postgres:postgres@db.example:5432/postgres",
    "postgresql://postgres:postgres@localhost:5433/postgres",
    "postgresql://postgres:postgres@localhost/postgres",
    "postgresql://other:postgres@localhost:5432/postgres",
    "postgresql://postgres:secret@localhost:5432/postgres",
    "postgresql://postgres:postgres@localhost:5432/postgres?sslmode=require",
    "postgres://postgres:postgres@localhost:5432/postgres",
    "postgresql://postgres:postgres@localhost:5432",
])
def test_admin_url_rejects_operator_remote_and_other_servers(url):
    with pytest.raises(ValueError, match="disposable local database"):
        require_disposable_database(url, served=False)


@pytest.mark.parametrize("url", [
    "postgresql://postgres:postgres@localhost:5432/looper_eval_abc",
    "postgresql://postgres:postgres@127.0.0.1:54329/looper_eval_abc",
])
def test_served_url_accepts_a_fresh_eval_database_on_either_disposable_server(url):
    require_disposable_database(url, served=True)


@pytest.mark.parametrize("url", [
    _CI_ADMIN,
    _LOCAL_ADMIN,
    "postgresql://postgres:postgres@localhost:5432/loopx",
    "postgresql://postgres:postgres@localhost:5432/looper_eval_abc?x=1",
    "postgresql://postgres:postgres@db.example:5432/looper_eval_abc",
    "postgresql://postgres:secret@127.0.0.1:54329/looper_eval_abc",
])
def test_served_url_rejects_anything_but_a_fresh_eval_database(url):
    with pytest.raises(ValueError, match="disposable local database"):
        require_disposable_database(url, served=True)


def test_isolated_serve_tools_host_charge_and_pre_request_denial(tmp_path):
    import looper

    print(sys.executable, looper.__file__)
    assert Path(looper.__file__).resolve() == ROOT / "looper/__init__.py"
    admin = os.environ.get("LOOPER_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("serve acceptance untested: disposable LOOPER_TEST_DATABASE_URL required")
    parts = urlsplit(admin)
    # Host, port, role and database name stay in the assertion. The password is
    # checked by require_disposable_database so a failure does not print it.
    assert (parts.scheme, parts.hostname, parts.port, parts.username) in DISPOSABLE_SERVERS
    name = unquote(parts.path.lstrip("/"))
    assert name and name not in OPERATOR_DATABASES, "operator database is forbidden"
    assert not parts.query and not parts.fragment
    require_disposable_database(admin, served=False)
    log = tmp_path / "serve.log"
    with ExitStack() as resources:
        try:
            url, _ = resources.enter_context(fresh_database(admin))
        except EvalError:
            pytest.skip("serve acceptance untested: disposable PostgreSQL is unavailable or lacks CREATEDB")
        with serving(database_url=url, runtime_root=tmp_path / "runtime", home=tmp_path / "home",
                     work_dir=tmp_path / "work", log_path=log) as api:
            profile = seed(url, api)
            assert not any(row["enabled"] for row in query(url, "SELECT enabled FROM channel_configs"))
            api.post("/api/v1/agent_settings/update_role", {"role": "manager", "values": {
                "profile_id": profile, "run_budget_usd": 1}})
            reply, envelope_id = ask(api, "runner-allowed")
            assert FACT in reply
            (record,) = query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")
            assert record["status"] == "completed", record["error"]
            assert FACT not in record["input_text"]  # The archived fact is absent from the injected ready tasks.
            assert record["cost_source"] == "provider_price"
            assert Decimal(str(record["cost_usd"])) == Decimal("0.0039")
            assert tuple(record[k] for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                                             "cache_creation_tokens")) == (1000, 200, 300, 400)
            saved = events(url, record)
            (result,) = [e for e in saved if e["type"] == "result"]
            trace, session = result["trace_id"], result["session_id"]
            assert all(e["trace_id"] == trace and e["session_id"] == session for e in saved)
            assert result["runner"]["outcome"] == "completed" and result["num_turns"] == 1
            assert result["total_cost_usd"] == 0.0039 and result["usage_complete"]
            # Internal usage events feed accounting but are deliberately not persisted.
            assert result["usage"] == {"input_tokens": 1000, "output_tokens": 200,
                "cache_read_input_tokens": 300, "cache_creation_input_tokens": 400}
            (call,) = [b for e in saved if e["type"] == "assistant"
                       for b in e["message"]["content"] if b["type"] == "tool_use"]
            (returned,) = [b for e in saved if e["type"] == "user"
                           for b in e["message"]["content"] if b["type"] == "tool_result"]
            assert (call["name"], call["input"]) == ("looper_tasks", {"action": "list"})
            assert returned["tool_use_id"] == call["id"] and not returned["is_error"]
            assert FACT in returned["content"] and returned["content"] in reply
            (action,) = query(url, "SELECT * FROM manager_actions WHERE tool='looper_tasks'")
            assert envelope_id and (action["status"], action["channel"], action["message_id"], action["chat"]) == (
                "ok", "web", envelope_id, "runner-acceptance")
            assert json.loads(action["arguments"]) == call["input"]
            assert action["manager_id"] == "manager" and action["risk"] == "read"
            assert not query(url, "SELECT id FROM llm_records WHERE agent_id <> 'manager'")

            api.post("/api/v1/agent_settings/update_role", {"role": "manager", "values": {
                "run_budget_usd": 0.0001}})
            capped_reply, _ = ask(api, "runner-denied")
            assert FACT not in capped_reply
            records = query(url, "SELECT * FROM llm_records WHERE agent_id='manager' ORDER BY id")
            assert len(records) == 2
            denied = events(url, records[1])
            (capped,) = [e for e in denied if e["type"] == "result"]
            assert capped["runner"]["outcome"] == "capped" and capped["num_turns"] == 0
            assert capped["runner"]["cap_reason"] == "run_cap"
            assert records[1]["cost_usd"] == 0
            assert not any(e["type"] in ("usage", "assistant", "user") for e in denied)
            assert len(query(url, "SELECT id FROM manager_actions WHERE tool='looper_tasks'")) == 1
            assert sum(Decimal(str(r["cost_usd"])) for r in records) == Decimal("0.0039")
        diagnostics = log.read_text()
        assert KEY not in diagnostics
        assert str(ROOT / "looper/__init__.py") in diagnostics and sys.executable in diagnostics
        wire = [json.loads(line.split("runner fake stderr: ", 1)[1])
                for line in diagnostics.splitlines() if "runner fake stderr: {" in line]
        allowed = [e for e in wire if e["trace_id"] == trace]
        assert [e["event"] for e in allowed] == [
            "budget/authorize", "authorization", "tools/call", "request_end", "settled"]
        assert allowed[1]["allow"] is True
        assert allowed[0]["request_id"] == allowed[1]["request_id"] == allowed[3]["request_id"]
        assert allowed[3]["claimed_cost_usd"] == 999  # SDK dollars cannot replace host prices.
        assert tuple(allowed[3][k] for k in ("input_uncached", "output", "cache_read", "cache_write")) == (
            1000, 200, 300, 400)
        assert all(e["session_id"] == session for e in allowed)
        denied_wire = [e for e in wire if e["trace_id"] == capped["trace_id"]]
        assert [e["event"] for e in denied_wire] == ["budget/authorize", "authorization", "settled"]
        assert denied_wire[1]["allow"] is False and denied_wire[1]["reason"] == "run_cap"
        assert denied_wire[0]["request_id"] != allowed[0]["request_id"]
        assert denied_wire[2]["outcome"] == "capped" and denied_wire[2]["requests"] == 0
        cleanup = json.loads(diagnostics.split("runner test cleanup=", 1)[1])
        assert len(cleanup["runners"]) == 1 and cleanup["normal_stop"]
        assert not cleanup["stopped_after_sigint"] and not cleanup["after_cleanup"]
