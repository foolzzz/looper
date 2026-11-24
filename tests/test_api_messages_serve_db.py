"""Messages on the actual CLI server, synthetic HTTP upstream and disposable PG."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import time

import asyncpg
import pytest

from evals.prompt_regression import runner
from looper.agents.runtimes.api.session import FORBIDDEN_ENV
from tests.db_support import DSN, fresh_database_without_recording
from tests.fake_model_api import FakeModelAPI, ResponseScript, message_call_events, message_events

pytestmark = pytest.mark.skipif(not DSN, reason="LOOPER_TEST_DATABASE_URL is not set")


def test_messages_cli_serve_confirmation_stop_and_restart(tmp_path, monkeypatch):
    for name in FORBIDDEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runner, "_SERVE", "import sys, looper\nfrom looper.cli import main\n"
                        'print("acceptance_python=" + sys.executable, flush=True)\n'
                        'print("acceptance_checkout=" + looper.__file__, flush=True)\n'
                        'sys.exit(main(["serve", "--host", "127.0.0.1", "--port", sys.argv[1], '
                        '"--runtime-root", sys.argv[2]]))\n')
    runtime, home, binaries = (tmp_path / name for name in ("runtime", "home", "bin"))
    home.mkdir()
    binaries.mkdir()
    # Discovery may ask about installed CLIs, but this acceptance never runs one.
    stub = f"#!{sys.executable}\nimport sys\nsys.exit(76)\n"
    for name in ("claude", "codex", "omp", "pi", "dsh", "grok", "cursor-agent", "agent", "ttadk", "coco", "lark-cli"):
        path = binaries / name
        path.write_text(stub)
        path.chmod(0o755)

    async def run(url):
        conn = await asyncpg.connect(url)
        await conn.execute("INSERT INTO system_settings (key,value) VALUES ('chat.stop_enabled','true')")
        sequence = 0
        async with FakeModelAPI([]) as model:
            @asynccontextmanager
            async def serve(index):
                context = runner.running_server(database_url=url, runtime_root=runtime, bin_dir=binaries,
                    home=home, notes_root=home / "notes", startup_timeout=60,
                    log_path=tmp_path / f"serve-{index}.log", env={"LOOPER_TELEGRAM_BOT_TOKEN": ""})
                api = await asyncio.to_thread(context.__enter__)
                try:
                    assert int(api.base_url.rsplit(":", 1)[1]) != 8767
                    yield api
                finally:
                    await asyncio.to_thread(context.__exit__, *sys.exc_info())

            async def post(api, path, body):
                return await asyncio.to_thread(api.post, "/api/v1/" + path, body)

            async def ask(api, text, expected):
                nonlocal sequence
                sequence += 1
                message_id = f"messages-{sequence}"
                await post(api, "operator_chat/send", {"text": text, "session_id": "messages-chat", "message_id": message_id})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    messages = (await post(api, "operator_chat/messages", {"session_id": "messages-chat"}))["result"]["messages"]
                    reply = next((m for m in messages if m.get("message_id") == message_id + ":reply"), None)
                    if reply is not None:
                        assert expected in reply["content"], reply
                        return reply
                    await asyncio.sleep(.05)
                raise AssertionError("the synthetic Messages chat did not finish")

            def answer(text):
                model.scripts.append(ResponseScript(events=message_events(text)))

            def call(name, arguments):
                model.scripts.append(ResponseScript(events=message_call_events(json.dumps(arguments), name=name,
                    call_id=f"call_{len(model.scripts)}")))

            try:
                async with serve(0) as api:
                    registry = (await post(api, "registry/view", {}))["result"]
                    runtime_row = next(r for r in registry["runtimes"] if r["type"] == "api")
                    assert runtime_row["roles"] == ["manager"] and runtime_row["in_process"]
                    provider = (await post(api, "provider_accounts/create", {
                        "name": "Synthetic Messages account", "vendor_type": "custom", "api_key": "test-key",
                        "endpoints": [{"protocol": "anthropic-messages", "base_url": model.base_url}],
                    }))["result"]["provider"]
                    registered = (await post(api, "ops/models/create", {"provider": provider["provider_id"],
                        "protocol": "anthropic-messages", "name": "synthetic-messages[1m]", "efforts": [],
                        "context_window": 1_000_000, "input_usd_per_mtok": 2, "output_usd_per_mtok": 4,
                        "cache_read_usd_per_mtok": .5, "cache_write_usd_per_mtok": 3}))["result"]["model"]
                    profile = (await post(api, "profiles/create", {"name": "Synthetic Messages Manager",
                        "runtime_id": runtime_row["runtime_id"], "model_id": registered["model_id"]}))["result"]["profile"]
                    await post(api, "agent_settings/update_role", {"role": "manager", "values": {"profile_id": profile["profile_id"]}})
                    answer("Text path works.")
                    await ask(api, "Say hello.", "Text path works.")
                    assert model.requests[0].path == "/v1/messages"
                    assert model.requests[0].body["model"] == "synthetic-messages"
                    first_session = await conn.fetchval("SELECT session_id FROM llm_records WHERE agent_id='manager' ORDER BY id LIMIT 1")

                    call("looper_projects", {"action": "create", "name": "synthetic-project"})
                    answer("Created the project.")
                    await ask(api, "Create synthetic-project.", "Created the project.")
                    assert await conn.fetchval("SELECT count(*) FROM projects WHERE name='synthetic-project'") == 1
                    call("looper_projects", {"action": "delete", "project": "synthetic-project"})
                    answer("Please confirm deletion.")
                    await ask(api, "Delete synthetic-project.", "Please confirm deletion.")
                    old_confirmation = await conn.fetchval("SELECT confirmation_id FROM manager_actions "
                        "WHERE tool='looper_projects' AND status='needs_confirmation' ORDER BY id DESC LIMIT 1")
                    assert old_confirmation
                    answer("Cancelled. The project remains.")
                    await ask(api, "Cancel that deletion.", "Cancelled.")
                    assert await conn.fetchval("SELECT count(*) FROM projects WHERE name='synthetic-project'") == 1
                    call("looper_confirm", {"confirmation_id": old_confirmation})
                    answer("The cancelled confirmation cannot run.")
                    await ask(api, "Check the old confirmation without changing anything.", "cannot run.")
                    assert await conn.fetchval("SELECT count(*) FROM projects WHERE name='synthetic-project'") == 1
                    assert await conn.fetchval("SELECT status FROM manager_actions WHERE tool='looper_confirm' ORDER BY id DESC LIMIT 1") == "error"

                    call("looper_projects", {"action": "delete", "project": "synthetic-project"})
                    answer("Please confirm again.")
                    await ask(api, "Delete synthetic-project again.", "Please confirm again.")
                    confirmation = await conn.fetchval("SELECT confirmation_id FROM manager_actions "
                        "WHERE tool='looper_projects' AND status='needs_confirmation' ORDER BY id DESC LIMIT 1")
                    assert confirmation != old_confirmation
                    call("looper_confirm", {"confirmation_id": confirmation})
                    answer("Confirmed deletion.")
                    await ask(api, "Confirm deletion.", "Confirmed deletion.")
                    assert await conn.fetchval("SELECT count(*) FROM projects WHERE name='synthetic-project'") == 0

                    before_stop = await conn.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", first_session)
                    blocked = ResponseScript(events=message_events("Partial stopped answer."), gate=asyncio.Event())
                    model.scripts.append(blocked)
                    await post(api, "operator_chat/send", {"text": "Wait until stopped.", "session_id": "messages-chat", "message_id": "messages-stop"})
                    await asyncio.wait_for(blocked.gate_reached.wait(), 15)
                    turn = (await post(api, "operator_chat/turn", {"session_id": "messages-chat"}))["result"]["turn"]
                    assert turn["can_interrupt"] is True
                    await post(api, "operator_chat/stop", {"message_id": turn["message_id"]})
                    await asyncio.wait_for(blocked.closed.wait(), 5)
                    deadline = time.monotonic() + 10
                    while True:
                        row = await conn.fetchrow("SELECT status,cost_usd,metadata,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens "
                            "FROM llm_records WHERE agent_id='manager' ORDER BY id DESC LIMIT 1")
                        if row["status"] == "interrupted" and "user_stop" in json.loads(row["metadata"]):
                            break
                        assert time.monotonic() < deadline
                        await asyncio.sleep(.05)
                    assert row["cost_usd"] is None and json.loads(row["metadata"])["user_stop"]["usage_complete"] is False
                    assert tuple(row[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (11, 5, 3, 2)
                    assert await conn.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", first_session) == before_stop
                    runs = (await post(api, "runs/list", {"agent_id": "manager"}))["result"]["records"]
                    assert all(r["metadata"]["api"]["protocol"] == "anthropic-messages" for r in runs)
                    assert all(r["session_id"] == first_session for r in runs)
                    assert any(r["metadata"]["api"]["upstream_ids"] for r in runs)
                    completed = [r for r in runs if r["status"] == "completed"]
                    assert completed and all(r["cost_usd"] > 0 for r in completed)
                    for record in completed:
                        count = record["metadata"]["api"]["requests"]
                        assert tuple(record[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")) == (
                            11 * count, 5 * count, 3 * count, 2 * count)
                        assert record["cost_usd"] == pytest.approx(.0000495 * count)
                        assert record["metadata"]["api"]["upstream_ids"] == ["msg_1"] * count
                    assert "test-key" not in json.dumps(runs)

                answer("Resumed after restart.")
                async with serve(1) as api:
                    await ask(api, "Continue the same conversation.", "Resumed after restart.")
                    last = await conn.fetchrow("SELECT session_id,status FROM llm_records WHERE agent_id='manager' ORDER BY id DESC LIMIT 1")
                    assert dict(last) == {"session_id": first_session, "status": "completed"}
                    sent = model.requests[-1].body["messages"]
                    blocks = [block for message in sent for block in message["content"]]
                    assert any(b.get("type") == "tool_use" and b["name"] == "looper_projects" for b in blocks)
                    assert any(b.get("type") == "text" and "Text path works." in b["text"] for b in blocks)
                    assert not any(b.get("type") == "text" and "Partial stopped answer." in b["text"] for b in blocks)
                    await ask(api, "/new", "")
                    assert await conn.fetchval("SELECT count(*) FROM api_session_items WHERE session_id=$1", first_session) == 0
                    assert await conn.fetchval("SELECT count(*) FROM api_sessions WHERE session_id=$1", first_session) == 0
                assert len(model.requests) == len(model.scripts)
                import looper
                for index in (0, 1):
                    diagnostics = (tmp_path / f"serve-{index}.log").read_text()
                    assert "acceptance_python=" + sys.executable in diagnostics
                    assert "acceptance_checkout=" + str(Path(looper.__file__).resolve()) in diagnostics
            finally:
                await conn.close()

    with fresh_database_without_recording(str(DSN)) as (url, _):
        asyncio.run(run(url))
