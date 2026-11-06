"""The API Manager records the SDK's unchanged HTTP request under its turn."""

import json

import pytest

from tests.api_manager_support import USAGE, _manager, _message, _replies
from tests.db_support import DSN, run_db
from tests.fake_model_api import ResponseScript, text_events
from tests.test_llmrec_resident_db import _recording

pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable LOOPER_TEST_DATABASE_URL")


@pytest.mark.parametrize("enabled", [True, False], ids=["recording-on", "recording-off"])
def test_api_manager_records_the_sdk_request_and_reuses_route_after_restart(monkeypatch, tmp_path, enabled):
    from looper.agents.llmrec import LlmrecRoute
    from looper.db import llm_requests

    async def body(pool):
        scripts = [ResponseScript(events=text_events(answer, usage=USAGE)) for answer in ("First.", "Second.")]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            async with _recording(pool, tmp_path, enabled=enabled) as (proxy, sink):
                routes = []
                for index in range(2):
                    await chat.manager._handle(_message("Reply briefly.", index))
                    routes.append(chat.manager._process.launch.llmrec)
                    assert chat.manager._claude._descriptor.base_url == chat.model.base_url
                    assert chat.manager._process.launch.record()["api"]["base_url_host"] == "127.0.0.1"
                    # In-process runtime restarts must not replace this resident agent's route.
                    if index == 0:
                        await chat.manager._claude.stop()
                assert [reply.payload["text"] for reply in _replies(chat.bus)] == ["First.", "Second."]
                await proxy.close()
                await sink.close(timeout=5)
                records = await pool.fetch("SELECT id, metadata FROM llm_records WHERE agent_id='manager' ORDER BY id")
                assert len(records) == len(chat.model.requests) == 2
                assert routes[0] == routes[1]
                if enabled:
                    assert isinstance(routes[0], LlmrecRoute)
                    for record, upstream in zip(records, chat.model.requests, strict=True):
                        metadata = json.loads(record["metadata"])
                        assert metadata["llmrec"]["state"] == "recording"
                        assert metadata["llmrec"]["route"] == routes[0].id
                        requests = await llm_requests.list_requests(record_id=record["id"], pool=pool)
                        assert len(requests) == 1
                        saved = await llm_requests.get_request(requests[0]["id"], pool=pool)
                        assert saved["request_body"] == upstream.body
                        assert upstream.path == "/v1/responses"
                        assert saved["request_headers"]["authorization"] == "<redacted>"
                else:
                    assert routes == [None, None]
                    assert all("llmrec" not in json.loads(record["metadata"]) for record in records)
                    assert await pool.fetchval("SELECT count(*) FROM llm_requests") == 0

    run_db(body, monkeypatch, timeout=50)


def test_api_manager_handoff_records_the_request_under_its_own_turn(monkeypatch, tmp_path):
    from looper.agents import session_switch
    from looper.agents.runtimes.cancel import TurnCancel

    async def body(pool):
        scripts = [ResponseScript(events=text_events(answer, usage=USAGE)) for answer in ("First.", "Handoff.")]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            async with _recording(pool, tmp_path) as (proxy, sink):
                await chat.manager._handle(_message("Reply briefly."))
                process = chat.manager._process
                switch = session_switch.context_full(200_001, 200_000, runtime="api",
                    profile=process.launch.profile.id, provider=process.launch.profile.provider.id)
                handoff = await chat.manager._handoff(switch, process, TurnCancel.never())
                assert handoff == "Handoff."
                await proxy.close()
                await sink.close(timeout=5)
                records = await pool.fetch("SELECT id FROM llm_records WHERE agent_id='manager' ORDER BY id")
                requests = await pool.fetch("SELECT record_id, route_id FROM llm_requests ORDER BY id")
                assert len(records) == len(requests) == len(chat.model.requests) == 2
                assert [row["record_id"] for row in requests] == [row["id"] for row in records]
                assert {row["route_id"] for row in requests} == {process.launch.llmrec.id}

    run_db(body, monkeypatch, timeout=40)


def test_manager_autonomous_turn_keeps_early_request_between_turns(monkeypatch, tmp_path):
    import httpx

    from looper.agents.claude_cli import ResidentTurn
    from looper.agents.run_usage import Usage

    async def body(pool):
        scripts = [ResponseScript(events=text_events("ok", usage=USAGE)) for _ in range(3)]
        async with _manager(pool, tmp_path, monkeypatch, scripts) as chat:
            async with _recording(pool, tmp_path) as (proxy, sink):
                await chat.manager._handle(_message("Reply briefly."))
                process = chat.manager._process
                route = process.launch.llmrec
                # The model can start its own request before the first output event triggers the callback.
                async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
                    async def request():
                        response = await client.post(route.url + "/responses",
                            headers={"authorization": "Bearer test-key"},
                            json={"model": "test-model", "input": "Autonomous lookup.", "stream": True})
                        response.raise_for_status()

                    await request()
                    begin = chat.manager._autonomous_turns(process.command, process.system_prompt, process.launch)
                    autonomous = await begin(None)
                    await request()
                await autonomous.event({"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "Autonomous."}]}})
                result = {"type": "result", "subtype": "success", "is_error": False,
                          "result": "Autonomous.", "session_id": chat.manager._session_id}
                await autonomous.event(result)
                await autonomous.end(ResidentTurn(result_event=result, usage=Usage(input_tokens=3, output_tokens=2)))
                await proxy.close()
                await sink.close(timeout=5)
                records = await pool.fetch("SELECT id, metadata FROM llm_records WHERE agent_id='manager' ORDER BY id")
                requests = await pool.fetch("SELECT record_id, after_record_id, route_id FROM llm_requests ORDER BY id")
                assert len(records) == 2 and len(requests) == len(chat.model.requests) == 3
                assert json.loads(records[1]["metadata"])["autonomous"] is True
                assert [(row["record_id"], row["after_record_id"]) for row in requests] == [
                    (records[0]["id"], None), (None, records[0]["id"]), (records[1]["id"], None)]
                assert {row["route_id"] for row in requests} == {route.id}

    run_db(body, monkeypatch, timeout=40)
