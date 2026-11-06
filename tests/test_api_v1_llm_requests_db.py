"""Only the operator may change recording or read model exchanges."""
from datetime import datetime, timezone
import secrets


from tests.db_support import run_db, v1_api


def test_recording_operations_are_operator_only_and_validate_shapes(tmp_path, monkeypatch):
    from looper.agents import api_v1
    from looper.agents.llmrec import RouteFile
    from looper.agents.llmrec.capture import Capture, split_request
    from looper.agents.llmrec.control import install
    from looper.db import llm_requests

    async def check(pool):
        install(None)
        route = RouteFile(id=secrets.token_urlsafe(16), upstream="http://localhost:1234/base",
            agent_id="worker-test", created_at=datetime.now(timezone.utc).isoformat())
        body = {"system": "synthetic", "tools": [], "messages": [{"role": "user", "content": "hello"}]}
        skeleton, parts = split_request(body, "/v1/messages")
        capture = Capture(route=route, instance="test", seq=1, started_at=datetime.now(timezone.utc),
            method="POST", path="/v1/messages", kind="model", outcome="completed", status=200,
            skeleton=skeleton, parts=parts, request_bytes=1, request_sha256="synthetic")
        await llm_requests.write_batch([capture], pool=pool)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        async with v1_api(tmp_path, pool) as call:
            operations = [("llmrec_settings.get", {}), ("llmrec_settings.update", {"enabled": True}),
                ("llm_requests.list", {}), ("llm_requests.get", {"request_id": identifier})]
            for name, arguments in operations:
                code, _ = await call(name, arguments, client=True)
                assert code == 403
            for enabled, include_oauth in [(True, False), (True, True), (False, False)]:
                code, reply = await call("llmrec_settings.update", {"enabled": enabled, "include_oauth": include_oauth})
                assert code == 200, reply
                assert reply["result"]["enabled"] is enabled
                assert reply["result"]["include_oauth"] is include_oauth
                code, current = await call("llmrec_settings.get")
                assert current["result"] == reply["result"]
            for name, arguments in (operations[0], *operations[2:]):
                code, reply = await call(name, arguments)
                assert code == 200, reply
                operation = api_v1.find("POST", "/api/v1/" + name.replace(".", "/"))
                api_v1.validate_arguments(operation.result, reply["result"])
            _, reply = await call("llm_requests.get", {"request_id": identifier})
            assert reply["result"]["request"]["request_body"] == body
            _, reply = await call("llm_requests.list")
            assert "request_body" not in reply["result"]["requests"][0]
            code, _ = await call("llm_requests.get", {"request_id": identifier + 999})
            assert code == 404
            code, _ = await call("llm_requests.list", {"between_turns": True})
            assert code == 400
            code, _ = await call("llmrec_settings.update", {"enabled": "true"})
            assert code == 400
    run_db(check, monkeypatch)


def test_recording_cli_uses_closed_operator_schemas():
    from types import SimpleNamespace
    from looper import loopercli
    from looper.agents import api_v1

    cases = [(['llmrec-settings', 'get'], '/api/v1/llmrec_settings/get', {}),
        (['llmrec-settings', 'update', '--enabled', '--include-oauth'], '/api/v1/llmrec_settings/update',
         {'enabled': True, 'include_oauth': True}),
        (['llm-requests', 'list', '--record-id', '7', '--between-turns'], '/api/v1/llm_requests/list',
         {'record_id': 7, 'between_turns': True}),
        (['llm-requests', 'get', '--request-id', '9'], '/api/v1/llm_requests/get', {'request_id': 9})]
    for argv, path, expected in cases:
        args = loopercli.build_parser(api_v1.document()).parse_args(argv)
        calls = []
        api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {'status': 'ok', 'result': {}})))
        assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session='recording', operator=True)) == 0
        assert calls == [('POST', path, expected)]
        operation = api_v1.find('POST', path)
        assert operation.operator_only and operation.request['additionalProperties'] is False


def test_recording_self_check_statuses(monkeypatch):
    import asyncio
    from looper.agents import self_check
    from looper.agents.llmrec import control

    class Status:
        def __init__(self, enabled, state, dropped=0, write_errors=0):
            self.value = {'enabled': enabled, 'proxy': {'state': state, 'dropped': dropped,
                                                      'write_errors': write_errors}}

        async def status(self):
            return self.value

    for arguments, expected in [((False, 'stopped'), self_check.SKIP),
        ((True, 'unavailable'), self_check.FAIL), ((True, 'outdated'), self_check.WARN),
        ((True, 'running', 1, 0), self_check.WARN), ((True, 'running', 0, 1), self_check.WARN),
        ((True, 'running'), self_check.OK)]:
        monkeypatch.setattr(control, '_control', Status(*arguments))
        assert asyncio.run(self_check.llmrec_check()).status == expected
