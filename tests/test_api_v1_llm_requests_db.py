"""Only the operator may change recording or read model exchanges."""
from datetime import datetime, timezone
from dataclasses import replace
import secrets

import pytest


from tests.db_support import run_db, v1_api


@pytest.mark.parametrize('enabled', [None, 'false', 'true'])
def test_recording_settings_without_controller_default_on(tmp_path, monkeypatch, enabled):
    from looper.agents.llmrec import control
    from looper.db import system_settings

    async def check(pool):
        monkeypatch.setattr(control, '_control', None)
        if enabled is not None:
            await system_settings.put_settings({'llmrec.enabled': enabled}, pool=pool)
        async with v1_api(tmp_path, pool) as call:
            code, reply = await call('llmrec_settings.get')
            assert code == 200, reply
            result = reply['result']
            assert result['enabled'] is (enabled != 'false')
            assert result['include_oauth'] is False
            assert result['proxy']['state'] == ('stopped' if enabled == 'false' else 'unavailable')
    run_db(check, monkeypatch, timeout=15)


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
                ("llm_requests.list", {}), ("llm_requests.get", {"request_id": identifier}),
                ("llm_requests.export", {"agent_id": route.agent_id}),
                ("llm_requests.purge", {"all": True})]
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
            for name, arguments in (operations[0], *operations[2:-1]):
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
        (['llmrec-settings', 'update', '--enabled', 'false'], '/api/v1/llmrec_settings/update',
         {'enabled': False}),
        (['llmrec-settings', 'update', '--enabled', 'true'], '/api/v1/llmrec_settings/update',
         {'enabled': True}),
        (['llmrec-settings', 'update', '--no-enabled'], '/api/v1/llmrec_settings/update',
         {'enabled': False}),
        (['llm-requests', 'list', '--record-id', '7', '--between-turns'], '/api/v1/llm_requests/list',
         {'record_id': 7, 'between_turns': True}),
        (['llm-requests', 'get', '--request-id', '9'], '/api/v1/llm_requests/get', {'request_id': 9}),
        (['llmrec-settings', 'update', '--retention-days', '365', '--max-total-mb', '102.4'],
         '/api/v1/llmrec_settings/update', {'retention_days': 365, 'max_total_mb': 102.4}),
        (['llm-requests', 'purge', '--all'], '/api/v1/llm_requests/purge', {'all': True})]
    for argv, path, expected in cases:
        args = loopercli.build_parser(api_v1.document()).parse_args(argv)
        calls = []
        api = SimpleNamespace(call=lambda *parts: (calls.append(parts) or (200, {'status': 'ok', 'result': {}})))
        assert args._run(args, SimpleNamespace(api=lambda: api, as_json=True, session='recording', operator=True)) == 0
        assert calls == [('POST', path, expected)]
        operation = api_v1.find('POST', path)
        assert operation.operator_only and operation.request['additionalProperties'] is False


@pytest.mark.parametrize('value', ['1', 'null', 'off'])
def test_recording_cli_rejects_non_boolean_enabled(value):
    from looper import loopercli
    from looper.agents import api_v1

    with pytest.raises(loopercli.UsageError):
        loopercli.build_parser(api_v1.document()).parse_args(['llmrec-settings', 'update', '--enabled', value])


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

    from unittest.mock import AsyncMock
    monkeypatch.setattr(self_check, '_llmrec_storage', AsyncMock(return_value=(0, 2048)))
    for arguments, expected in [((False, 'stopped'), self_check.SKIP),
        ((True, 'unavailable'), self_check.FAIL), ((True, 'outdated'), self_check.WARN),
        ((True, 'running', 1, 0), self_check.WARN), ((True, 'running', 0, 1), self_check.WARN),
        ((True, 'running'), self_check.OK)]:
        monkeypatch.setattr(control, '_control', Status(*arguments))
        assert asyncio.run(self_check.llmrec_check()).status == expected


def _capture(route, seq, body, *, path="/v1/messages", **values):
    from looper.agents.llmrec.capture import Capture, canonical, split_request

    route = replace(route, created_at=route.created_at or datetime.now(timezone.utc).isoformat())
    skeleton, parts = split_request(body, path)
    return Capture(route=route, instance="export-test", seq=seq,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), path=path,
        skeleton=skeleton, parts=parts, request_bytes=len(canonical(body)), request_sha256="synthetic",
        request_headers={"authorization": "<redacted>", "content-type": "application/json"},
        response_body={"content": [{"type": "text", "text": "reply"}]}, response_raw="raw response",
        response_headers={"content-type": "application/json"}, outcome="completed", status=200, **values)


def test_request_export_pages_filters_and_preserves_complete_exchanges(tmp_path, monkeypatch):
    from looper.agents import api_v1
    from looper.agents.llmrec import RouteFile
    from looper.db import llm_requests

    async def check(pool):
        route = RouteFile(id=secrets.token_urlsafe(16), upstream="https://example.invalid/api",
            agent_id="worker-export", task_id="task-export", step_id="step-export", role="worker",
            protocol="anthropic-messages", model="example-model", provider_id="example-provider")
        other = RouteFile(id=secrets.token_urlsafe(16), upstream="https://example.invalid",
            agent_id="worker-other", task_id="task-other")
        record_id = await pool.fetchval("INSERT INTO llm_records (agent_id) VALUES ($1) RETURNING id", route.agent_id)
        # Parts shared by the first and second page must still be reconstructed on each page.
        bodies = [{"system": "system", "messages": [{"role": "user", "content": "shared"}]
                   + [{"role": "assistant", "content": str(i)} for i in range(n)]} for n in range(3)]
        captures = [_capture(route, n + 1, body, record_id=record_id) for n, body in enumerate(bodies)]
        captures.append(_capture(other, 4, {"messages": [{"role": "user", "content": "other"}]}))
        captures.append(_capture(route, 5, {"messages": [{"role": "user", "content": "between"}]},
                                 after_record_id=record_id))
        alternate = replace(route, id=secrets.token_urlsafe(16))
        captures.append(_capture(alternate, 6, {"messages": [{"role": "user", "content": "new route"}]},
                                 record_id=record_id))
        await llm_requests.write_batch(captures, pool=pool)
        ids = [row["id"] for row in await pool.fetch("SELECT id FROM llm_requests ORDER BY id")]
        async with v1_api(tmp_path, pool) as call:
            code, listed = await call("llm_requests.list", {"record_id": record_id})
            assert code == 200, listed
            assert {row["id"] for row in listed["result"]["requests"]} == {*ids[:3], ids[5]}
            for filters, expected in [({"task_id": route.task_id}, [*ids[:3], ids[4], ids[5]]),
                                      ({"agent_id": other.agent_id}, [ids[3]]),
                                      ({"record_id": record_id}, [*ids[:3], ids[4], ids[5]]),
                                      ({"since": "2026-01-01T00:00:00Z"}, ids),
                                      ({"since": "2026-01-01", "until": "2026-01-02"}, ids),
                                      ({"since": "2026-01-02"}, [])]:
                seen, cursor = [], None
                while True:
                    arguments = {**filters, "limit": 2, **({"after_id": cursor} if cursor else {})}
                    code, reply = await call("llm_requests.export", arguments)
                    assert code == 200, reply
                    result = reply["result"]
                    api_v1.validate_arguments(api_v1.find("POST", "/api/v1/llm_requests/export").result, result)
                    for row in result["requests"]:
                        seen.append(row["id"])
                        capture = captures[ids.index(row["id"])]
                        if row["id"] in ids[:3]:
                            assert row["request_body"] == bodies[ids.index(row["id"])]
                        assert row["response_body"] == capture.response_body
                        assert row["response_raw"] == capture.response_raw
                        assert row["request_headers"]["authorization"] == "<redacted>"
                        assert row["route"]["id"] == capture.route.id
                        assert set(row["route"]) == {"id", "agent_id", "task_id", "step_id", "role", "runtime_type",
                            "provider_id", "protocol", "model", "upstream_host", "upstream_path"}
                        _, single = await call("llm_requests.get", {"request_id": row["id"]})
                        assert single["result"]["request"] == row
                    cursor = result["next_after_id"]
                    if cursor is None:
                        break
                assert seen == expected
            for invalid in [{}, {"until": "2026-01-02"}, {"agent_id": route.agent_id, "task_id": route.task_id},
                            {"since": "yesterday"}, {"since": "2026-01-02", "until": "2026-01-01"},
                            {"agent_id": route.agent_id, "limit": 51}, {"agent_id": ""}]:
                code, _ = await call("llm_requests.export", invalid)
                assert code == 400, invalid
        operation = api_v1.find("POST", "/api/v1/llm_requests/export")
        assert operation.operator_only and operation.hand and operation.risk == "read"
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("path,key", [("/v1/messages", "messages"),
    ("/v1/chat/completions", "messages"), ("/v1/responses", "input")])
def test_request_message_difference_uses_previous_capture_time(tmp_path, monkeypatch, path, key):
    from datetime import timedelta
    from looper.agents.llmrec import RouteFile
    from looper.db import llm_requests

    async def check(pool):
        route = RouteFile(id=secrets.token_urlsafe(16), upstream="https://example.invalid", agent_id="worker-diff")
        a = {"role": "user", "content": "a"}
        b = {"role": "assistant", "content": "b"}
        bodies = [{key: [a]}, {key: [a, b]}, {key: [{"role": "user", "content": "edited"}, b]}, "unsplit"]
        captures = [_capture(route, index + 1, body, path=path) for index, body in enumerate(bodies)]
        captures[0].started_at -= timedelta(seconds=1)
        captures[-1].record_gap = "unparsable_request"
        # The earlier capture was inserted later; tied timestamps are ordered by id.
        await llm_requests.write_batch([captures[1], captures[0], *captures[2:]], pool=pool)
        ids = {row["proxy_seq"]: row["id"] for row in await pool.fetch("SELECT id, proxy_seq FROM llm_requests")}
        async with v1_api(tmp_path, pool) as call:
            for seq, expected in [(1, None), (2, 1), (3, 0), (4, None)]:
                code, reply = await call("llm_requests.get", {"request_id": ids[seq]})
                assert code == 200, reply
                assert reply["result"]["request"]["new_from_index"] == expected
            code, reply = await call("llm_requests.export", {"agent_id": route.agent_id, "limit": 1})
            assert code == 200, reply
            assert reply["result"]["requests"][0]["new_from_index"] == 1
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("path,key", [("/v1/messages", "messages"),
    ("/v1/chat/completions", "messages"), ("/v1/responses", "input")])
def test_unchanged_and_prefix_retries_have_no_new_messages(tmp_path, monkeypatch, path, key):
    from datetime import timedelta
    from looper.agents.llmrec import RouteFile
    from looper.db import llm_requests

    async def check(pool):
        route = RouteFile(id=secrets.token_urlsafe(16), upstream="https://example.invalid", agent_id="worker-retry")
        a = {"role": "user", "content": "a"}
        b = {"role": "assistant", "content": "b"}
        bodies = [{key: messages} for messages in ([a, b], [a, b], [a], [], [a], [b])]
        captures = [_capture(route, index + 1, body, path=path) for index, body in enumerate(bodies)]
        for index, item in enumerate(captures):
            item.started_at = captures[0].started_at + timedelta(seconds=index)
        await llm_requests.write_batch(captures, pool=pool)
        async with v1_api(tmp_path, pool) as call:
            code, reply = await call("llm_requests.export", {"agent_id": route.agent_id})
            assert code == 200, reply
            exported = reply["result"]["requests"]
            assert len(exported) == len(bodies)
            for row, body, expected in zip(exported, bodies, [None, None, None, None, 0, 0], strict=True):
                assert row["request_body"] == body
                assert row["new_from_index"] == expected
                code, reply = await call("llm_requests.get", {"request_id": row["id"]})
                assert code == 200, reply
                assert reply["result"]["request"]["new_from_index"] == expected
    run_db(check, monkeypatch, timeout=30)


def test_missing_request_part_fails_without_partial_body(tmp_path, monkeypatch):
    from looper.agents.llmrec import RouteFile
    from looper.db import llm_requests

    async def check(pool):
        route = RouteFile(id=secrets.token_urlsafe(16), upstream="https://example.invalid", agent_id="worker-missing")
        await llm_requests.write_batch([_capture(route, 1, {"messages": [{"role": "user", "content": "private"}]})],
                                      pool=pool)
        identifier = await pool.fetchval("SELECT id FROM llm_requests")
        # Positive control: this request is complete before the deliberately damaged part is removed.
        async with v1_api(tmp_path, pool) as call:
            code, _ = await call("llm_requests.get", {"request_id": identifier})
            assert code == 200
            await pool.execute("DELETE FROM llm_request_parts WHERE route_id = $1", route.id)
            for operation, arguments in [("llm_requests.get", {"request_id": identifier}),
                                         ("llm_requests.export", {"agent_id": route.agent_id})]:
                code, reply = await call(operation, arguments)
                assert code == 500, reply
                assert str(identifier) in reply["error"]["message"]
                assert "private" not in str(reply) and "request_body" not in reply
    run_db(check, monkeypatch, timeout=30)


def test_recording_retention_settings_defaults_limits_and_reset(tmp_path, monkeypatch):
    from looper.agents.llmrec import control
    from looper.db import system_settings

    async def check(pool):
        monkeypatch.setattr(control, '_control', None)
        async with v1_api(tmp_path, pool) as call:
            code, reply = await call('llmrec_settings.get')
            assert code == 200, reply
            result = reply['result']
            assert result['enabled'] is True and result['include_oauth'] is False
            assert result['retention_days'] == {'value': 7, 'built_in': 7, 'source': 'built-in',
                                                'minimum': 1, 'maximum': 365}
            assert result['max_total_mb'] == {'value': 2048, 'built_in': 2048, 'source': 'built-in',
                                              'minimum': 102.4, 'maximum': 102400}
            assert result['stored_bytes'] > 0 and result['request_count'] == 0
            for name, value, span in [('retention_days', 0, '1 to 365'), ('retention_days', 366, '1 to 365'),
                                      ('max_total_mb', 102.3, '102.4 to 102400'),
                                      ('max_total_mb', 102401, '102.4 to 102400')]:
                code, reply = await call('llmrec_settings.update', {name: value, 'enabled': True})
                assert code == 400 and span in reply['error']['message'], reply
                # A rejected update must not change any field, including the switch.
                assert await system_settings.get_settings(['llmrec.enabled']) == {}
            for days, size in [(1, 102.4), (365, 102400)]:
                code, reply = await call('llmrec_settings.update', {'retention_days': days, 'max_total_mb': size})
                assert code == 200, reply
                assert reply['result']['retention_days']['value'] == days
                assert reply['result']['max_total_mb']['value'] == size
                assert reply['result']['max_total_mb']['source'] == 'setting'
            code, reply = await call('llmrec_settings.update', {'retention_days': None, 'max_total_mb': None})
            assert code == 200, reply
            assert reply['result']['retention_days'] == result['retention_days']
            assert reply['result']['max_total_mb'] == result['max_total_mb']
            assert await system_settings.get_settings(['llmrec.retention_days', 'llmrec.max_total_mb']) == {}
    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize('scope', ['task', 'agent', 'all'])
def test_request_purge_confirms_exact_scope_and_reads_back(tmp_path, monkeypatch, scope):
    from looper.agents import api_v1
    from looper.agents.llmrec import RouteFile, control
    from looper.agents.v1_domains import llm_requests as domain
    from looper.db import llm_requests

    async def check(pool):
        monkeypatch.setattr(control, '_control', None)
        routes = [RouteFile(id=secrets.token_urlsafe(16), upstream='https://example.invalid',
                            agent_id=agent, task_id=task)
                  for agent, task in [('worker-selected', 'task-selected'), ('worker-other', 'task-other')]]
        await llm_requests.write_batch([_capture(route, index + 1, {'system': 'shared', 'messages': []})
                                        for index, route in enumerate(routes)], pool=pool)
        arguments = {'task_id': 'task-selected'} if scope == 'task' else (
            {'agent_id': 'worker-selected'} if scope == 'agent' else {'all': True})
        async with v1_api(tmp_path, pool) as call:
            code, reply = await call('llm_requests.purge', arguments, client=True)
            assert code == 403, reply
            for invalid in [{}, {'task_id': 'task-selected', 'all': True}, {'agent_id': 'worker-selected', 'all': True},
                            {'task_id': 'task-selected', 'agent_id': 'worker-selected'}, {'all': False}]:
                code, reply = await call('llm_requests.purge', invalid)
                assert code == 400, reply
            operation = api_v1.find('POST', '/api/v1/llm_requests/purge')
            assert operation.operator_only and operation.confirms and operation.risk == 'high'
            impact = await domain.impact(None, operation, arguments, api_v1.OPERATOR, {})
            expected = 2 if scope == 'all' else 1
            assert str(expected) in impact
            assert ('task-selected' if scope == 'task' else 'worker-selected' if scope == 'agent'
                    else 'all agents and tasks') in impact
            assert await pool.fetchval('SELECT count(*) FROM llm_requests') == 2
            # The operator confirms in the dashboard; server-side pending gates are for API clients.
            code, answer = await call('llm_requests.purge', arguments)
            assert code == 200, answer
            assert answer['result'] == {'requests': expected, 'routes': expected}
            assert await pool.fetchval('SELECT count(*) FROM llm_requests') == 2 - expected
            assert await pool.fetchval('SELECT count(*) FROM llm_request_routes') == 2 - expected
            if scope != 'all':
                remaining = await pool.fetchval('SELECT id FROM llm_requests')
                assert (await llm_requests.get_request(remaining))['request_body'] == {'system': 'shared', 'messages': []}
    run_db(check, monkeypatch, timeout=20)


@pytest.mark.parametrize('enabled', [True, False])
def test_recording_self_check_warns_near_storage_cap(monkeypatch, enabled):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from looper.agents import self_check
    from looper.agents.llmrec import control

    monkeypatch.setattr(control, '_control', SimpleNamespace(status=AsyncMock(return_value={
        'enabled': enabled, 'proxy': {'state': 'running' if enabled else 'stopped'}})))
    monkeypatch.setattr(self_check, '_llmrec_storage', AsyncMock(return_value=(1900 * 1024 * 1024, 2048)))
    result = asyncio.run(self_check.llmrec_check())
    assert result.status == self_check.WARN
    assert '1900.0 MiB' in result.detail and '2048 MiB' in result.detail
    monkeypatch.setattr(self_check, '_llmrec_storage', AsyncMock(return_value=(int(0.9 * 2048 * 1024 * 1024), 2048)))
    assert asyncio.run(self_check.llmrec_check()).status == (self_check.OK if enabled else self_check.SKIP)
