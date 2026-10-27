"""Operator-only Manager history cleanup contracts and disposable-PostgreSQL readback."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from looper import loopercli
from looper.agents import api_v1
from looper.agents.ops import OpError
from looper.agents.ops.clients import CLIENT_TOOLS, ClientOps
from looper.agents.ops.service import MANAGER_TOOLS
from looper.db import api_clients, chat, manager_actions, managers
from tests.db_support import run_db
from tests.test_api_v1_auth import CLIENT_TOKEN, _bearer, _send, _serving, clients  # noqa: F401
from tests.test_loopercli import Server, _send as cli_send
from tests.test_operator_auth import TOKEN

TARGET = "manager-retired"
THREAD = "retired-thread"
COUNTS = {"threads": 1, "chat_messages": 1, "llm_records": 1, "llm_record_events": 1, "manager_actions": 1}


def _operation(action):
    operation = api_v1.find("POST", f"/api/v1/manager_cleanup/{action}")
    assert operation is not None
    return operation


@pytest.mark.parametrize("action,risk", [("preview", "read"), ("purge", "high")])
def test_discovered_cleanup_contract_is_operator_only_and_outside_agent_tools(action, risk):
    operation = _operation(action)
    assert api_v1.domain_for(operation).__name__ == "looper.agents.v1_domains.manager_cleanup"
    assert operation.operator_only and not operation.credential_write and not operation.hand
    assert operation.tool is None and operation.risk == risk and operation.confirms is (action == "purge")
    assert operation.request["additionalProperties"] is False
    assert operation.request["required"] == ["manager_id"]
    assert operation.request["properties"]["manager_id"]["minLength"] == 1
    retention = operation.request["properties"]["retention_days"]
    assert retention["type"] == "integer" and retention["minimum"] == 30 and retention["maximum"] == 3650
    document = api_v1.document()["paths"][f"/manager_cleanup/{action}"]["post"]
    assert document["x-looper-operator-only"] is True and document["x-looper-risk"] == risk
    assert all("cleanup" not in tool.name for tool in (*MANAGER_TOOLS, *CLIENT_TOOLS))
    result = {"manager_id": TARGET, "deleted_at": None, "retention_days": 30, "eligible": False,
              "blockers": ["Not deleted"], "counts": COUNTS, "purged": False}
    assert operation.result["additionalProperties"] is False
    assert set(operation.result["required"]) == set(result)
    assert operation.result["properties"]["counts"]["additionalProperties"] is False
    api_v1.validate_arguments(operation.result, result)
    with pytest.raises(OpError):
        api_v1.validate_arguments(operation.result, {**result, "body": "must not be exposed"})


@pytest.mark.parametrize("action", ["preview", "purge"])
@pytest.mark.parametrize("body", [
    {}, {"manager_id": ""}, {"manager_id": 1}, {"manager_id": None},
    {"manager_id": TARGET, "retention_days": 29}, {"manager_id": TARGET, "retention_days": 3651},
    {"manager_id": TARGET, "retention_days": 30.5}, {"manager_id": TARGET, "retention_days": 30.0}, {"manager_id": TARGET, "retention_days": True},
    {"manager_id": TARGET, "retention_days": "30"}, {"manager_id": TARGET, "retention_days": None},
    {"manager_id": TARGET, "cutoff": "2000-01-01"},
])
def test_invalid_requests_stop_before_cleanup(tmp_path, monkeypatch, action, body):
    operation = _operation(action)
    run = AsyncMock(side_effect=AssertionError("invalid request reached cleanup"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)
    monkeypatch.setattr(manager_actions, "record", AsyncMock(return_value=1))

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            status, _, answer = await _send(server, f"POST {operation.path}", _bearer(TOKEN), json.dumps(body))
            assert status == 400 and answer["error"]["code"] == "invalid", answer
            run.assert_not_awaited()
    asyncio.run(check())


@pytest.mark.parametrize("action", ["preview", "purge"])
@pytest.mark.usefixtures("clients")
def test_client_cannot_request_cleanup_or_obtain_a_confirmation(tmp_path, monkeypatch, action):
    operation = _operation(action)
    run = AsyncMock(side_effect=AssertionError("client reached cleanup"))
    monkeypatch.setattr(api_v1.domain_for(operation), "run", run)

    async def check():
        async with _serving(tmp_path) as server:
            server._client_ops = ClientOps(SimpleNamespace())
            for raw in ("not json", json.dumps({"manager_id": TARGET})):
                status, _, answer = await _send(server, f"POST {operation.path}", _bearer(CLIENT_TOKEN), raw)
                assert status == 403 and answer["error"]["code"] == "forbidden"
                assert "confirmation_id" not in answer
            status, _, answer = await _send(server, "POST /api/v1/confirm", _bearer(CLIENT_TOKEN),
                                            json.dumps({"confirmation_id": operation.operation_id}))
            assert status == 404 and answer["error"]["code"] == "not_found"
            assert not server._client_ops._pending
            run.assert_not_awaited()
    asyncio.run(check())


class _Fleet:
    def __init__(self):
        self.blockers = []
        self.guards = []

    def get(self, manager_id):
        return None

    def cleanup_blockers(self, manager_id):
        assert manager_id == TARGET
        return list(self.blockers)

    @asynccontextmanager
    async def cleanup_guard(self, manager_id):
        self.guards.append(manager_id)
        yield


@asynccontextmanager
async def _api(tmp_path, pool, fleet=None):
    _, token = await api_clients.create("Cleanup validation", pool=pool)
    async with _serving(tmp_path) as server:
        server._client_ops = ClientOps(SimpleNamespace())
        server._managers = fleet or _Fleet()

        async def call(action, body=None, *, client=False):
            path = "/api/v1/manager_cleanup/" + action
            status, _, answer = await _send(server, "POST " + path,
                {**_bearer(token if client else TOKEN), "X-Looper-Session": "cleanup-validation"},
                json.dumps(body if body is not None else {"manager_id": TARGET}))
            if status == 200:
                api_v1.validate_arguments(_operation(action).result, answer["result"])
            return status, answer
        yield call, server


async def _seed(pool):
    await pool.execute("INSERT INTO managers (id, name, system_prompt, enabled, deleted_at) "
                       "VALUES ($1, 'Retired example', 'Help with example work.', false, "
                       "transaction_timestamp() - interval '31 days')", TARGET)
    await pool.execute("INSERT INTO chat_threads (session_id, manager_id) VALUES ($1, $2)", THREAD, TARGET)
    await pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content) "
                       "VALUES ($1, 'old-message', 'user', 'Private historical body')", THREAD)
    record = await pool.fetchval("INSERT INTO llm_records (agent_id, status, input_text) "
                                 "VALUES ($1, 'completed', 'Private historical run') RETURNING id", TARGET)
    await pool.execute("INSERT INTO llm_record_events (record_id, seq, event) "
                       "VALUES ($1, 1, '{\"text\":\"Private historical event\"}')", record)
    await manager_actions.record(channel="web", chat=THREAD, message_id="old-message", tool="old-action",
                                 arguments={"body": "Private historical action"}, risk="write", status="ok",
                                 manager_id=TARGET, pool=pool)


def test_operator_preview_is_read_only_and_purge_executes_once_with_tombstone_readback(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as (call, server):
            before = await pool.fetchval("SELECT count(*) FROM manager_actions")
            status, answer = await call("preview")
            assert status == 200, answer
            preview = answer["result"]
            assert preview["eligible"] and not preview["purged"] and preview["retention_days"] == 30
            assert preview["counts"] == COUNTS and preview["blockers"] == []
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == before + 1
            audit = await pool.fetchrow("SELECT manager_id, risk, status, arguments, result FROM manager_actions "
                                        "WHERE tool='manager_cleanup.preview'")
            assert audit["manager_id"] == "manager" and audit["risk"] == "read" and audit["status"] == "ok"
            assert json.loads(audit["arguments"]) == {"manager_id": TARGET, "retention_days": 30}
            assert audit["result"] is None
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE manager_id=$1", TARGET) == before
            status, answer = await call("purge")
            assert status == 200 and answer["status"] == "ok", answer
            assert answer["result"]["purged"] and answer["result"]["counts"] == COUNTS
            assert "confirmation_id" not in answer and not server._client_ops._pending
            status, after = await call("preview")
            assert status == 200 and after["result"]["eligible"]
            assert after["result"]["counts"] == {**dict.fromkeys(COUNTS, 0), "threads": 1}
            assert await managers.key_used(TARGET, pool=pool)
            assert await chat.thread_owner(THREAD, pool=pool) == TARGET
            status, _, history = await _send(server, "POST /api/v1/operator_chat/messages", _bearer(TOKEN),
                                              json.dumps({"manager": TARGET, "session_id": THREAD}))
            assert status == 200 and history["result"]["messages"] == []
            status, _, forbidden = await _send(server, "POST /api/v1/operator_chat/messages", _bearer(TOKEN),
                                               json.dumps({"session_id": THREAD}))
            assert status == 409 and forbidden["error"]["code"] == "conflict"
    run_db(check, monkeypatch, timeout=30)


def test_success_audit_preserves_counts_outside_purged_owner_without_bodies(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as (call, _):
            status, answer = await call("purge", {"manager_id": TARGET, "retention_days": 30})
            assert status == 200, answer
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE manager_id=$1", TARGET) == 0
            rows = await pool.fetch("SELECT * FROM manager_actions WHERE tool='manager_cleanup.purge'")
            assert len(rows) == 1
            audit = dict(rows[0])
            assert audit["manager_id"] == "manager" and audit["status"] == "ok" and audit["risk"] == "high"
            assert audit["client_id"] is None and audit["confirmation_id"] is None
            assert json.loads(audit["arguments"]) == {"manager_id": TARGET, "retention_days": 30}
            result = json.loads(audit["result"])
            assert result["manager_id"] == TARGET and result["retention_days"] == 30 and result["counts"] == COUNTS
            assert "Private historical" not in json.dumps(audit, default=str)
    run_db(check, monkeypatch, timeout=30)


def test_changed_eligibility_between_preview_and_purge_is_conflict_with_history_intact(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as (call, _):
            assert (await call("preview"))[1]["result"]["eligible"]
            await pool.execute("UPDATE llm_records SET status='running' WHERE agent_id=$1", TARGET)
            status, answer = await call("purge")
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
            assert await pool.fetchval("SELECT count(*) FROM llm_record_events") == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE manager_id=$1", TARGET) == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='manager_cleanup.purge' "
                                       "AND status='ok'") == 0
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("blocker", ["sync pending", "retirement pending", "live agent", "live turn", "retry pending"])
def test_runtime_retirement_blockers_refuse_purge_and_preserve_history(tmp_path, monkeypatch, blocker):
    async def check(pool):
        await _seed(pool)
        fleet = _Fleet()
        async with _api(tmp_path, pool, fleet) as (call, _):
            assert (await call("preview"))[1]["result"]["eligible"]
            fleet.blockers = [blocker]
            status, preview = await call("preview")
            assert status == 200 and not preview["result"]["eligible"] and blocker in preview["result"]["blockers"]
            status, answer = await call("purge")
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("action", ["preview", "purge"])
def test_generated_cli_uses_transient_operator_token_and_declared_request(tmp_path, capsys, action):
    def respond(handler, request):
        cli_send(handler, 200, api_v1.document() if request[1].endswith("openapi.json") else
                 {"status": "ok", "result": {"purged": action == "purge"}})
    server = Server(respond)
    try:
        token = tmp_path / "operator-token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        client_file = tmp_path / "missing-client.json"
        assert loopercli.main(["--client-file", str(client_file), "--json", "manager-cleanup", action,
                               "--manager-id", TARGET, "--retention-days", "45", "--url", server.url,
                               "--operator-token-file", str(token)]) == 0
        method, path, headers, sent = server.requests[-1]
        assert (method, path, sent) == ("POST", f"/api/v1/manager_cleanup/{action}",
                                       {"manager_id": TARGET, "retention_days": 45})
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert not client_file.exists()
    finally:
        server.close()


@pytest.mark.parametrize("state", ["idle_agent", "supervised_turn", "retiring", "sync", "retry", "pending"])
def test_real_fleet_requires_complete_retirement_including_suspended_agents(tmp_path, state):
    from looper.agents.bus import MessageBus
    from looper.agents.managers import ManagerFleet, SyncState
    from looper.agents.managers_api import ManagerDefinition
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address
    from looper.agents.tiers.manager import ManagerAgent, ManagerConfig

    async def check():
        bus = MessageBus(runtime_root=tmp_path)
        supervisor = SimpleNamespace(bus=bus, _agents={})
        fleet = ManagerFleet(platform=SimpleNamespace(), supervisor=supervisor, runtime_root=tmp_path,
                             url="http://127.0.0.1:1", config=ManagerConfig(work_dir=tmp_path), broadcaster=None)
        assert fleet.cleanup_blockers(TARGET) == []
        if state == "idle_agent":
            definition = ManagerDefinition.from_row({"id": TARGET, "name": "Retired example", "enabled": False,
                "system_prompt": "Help with example work.", "tools": "tasks", "task_scope": "own",
                "task_policy": "free"})
            agent = ManagerAgent(config=ManagerConfig(work_dir=tmp_path), runtime_root=tmp_path, definition=definition)
            assert not agent.process_started
            fleet._agents[TARGET] = agent
        elif state == "supervised_turn":
            supervisor._agents[TARGET] = SimpleNamespace()
        elif state == "retiring":
            fleet._retiring.add(TARGET)
        elif state == "sync":
            fleet._sync[TARGET] = SyncState(error="Retry required", attempt=1)
        elif state == "retry":
            fleet._retries[TARGET] = asyncio.get_running_loop().create_future()
        else:
            await bus.publish(MessageEnvelope.create(source=channel_address("web"), target=manager_address(TARGET),
                message_type=MessageType.USER_MESSAGE, payload={"text": "Public-safe queued example"}))
        try:
            assert fleet.cleanup_blockers(TARGET)
            assert fleet.cleanup_blockers("manager-neighbor") == []
        finally:
            fleet._agents.clear()
            supervisor._agents.clear()
            fleet._retiring.clear()
            fleet._sync.clear()
            for retry in fleet._retries.values():
                retry.cancel()
            fleet._retries.clear()
            bus.take_queue(TARGET)
            await bus.shutdown()
        assert fleet.cleanup_blockers(TARGET) == []
    asyncio.run(check())


def test_fleet_change_after_guard_entry_is_rechecked_before_database_deletion(tmp_path, monkeypatch):
    from looper.db import manager_cleanup as db_cleanup

    async def check(pool):
        await _seed(pool)
        fleet = _Fleet()
        purge = db_cleanup.purge

        async def change_before_database_lock(*args, **kwargs):
            assert fleet.guards == [TARGET]
            fleet.blockers = ["A turn became live before the database lock"]
            return await purge(*args, **kwargs)

        monkeypatch.setattr(db_cleanup, "purge", change_before_database_lock)
        async with _api(tmp_path, pool, fleet) as (call, _):
            status, answer = await call("purge")
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert "turn became live" in answer["error"]["message"]
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE tool='manager_cleanup.purge' "
                                       "AND status='ok'") == 0
    run_db(check, monkeypatch, timeout=30)


def test_database_failure_rolls_back_history_and_records_error_without_success_audit(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        await pool.execute("""
            CREATE FUNCTION refuse_record_delete() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Synthetic cleanup failure'; END $$;
            CREATE TRIGGER refuse_record_delete BEFORE DELETE ON llm_records
            FOR EACH ROW EXECUTE FUNCTION refuse_record_delete();
        """)
        async with _api(tmp_path, pool) as (call, _):
            status, answer = await call("purge")
            assert status == 503 and answer["error"]["code"] == "unavailable", answer
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
            assert await pool.fetchval("SELECT count(*) FROM llm_records WHERE agent_id=$1", TARGET) == 1
            assert await pool.fetchval("SELECT count(*) FROM llm_record_events") == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE manager_id=$1", TARGET) == 1
            audit = await pool.fetch("SELECT manager_id, status, error_code FROM manager_actions "
                                     "WHERE tool='manager_cleanup.purge'")
            assert [dict(row) for row in audit] == [
                {"manager_id": "manager", "status": "error", "error_code": "unavailable"}]
    run_db(check, monkeypatch, timeout=30)


def test_impact_uses_current_counts_and_discloses_permanent_loss_and_retained_identity(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as (_, server):
            operation = _operation("purge")
            domain = api_v1.domain_for(operation)
            impact = await domain.impact(server, operation, {"manager_id": TARGET}, api_v1.OPERATOR, {})
            assert TARGET in impact and "30" in impact
            assert all(word in impact.lower() for word in ("message", "run", "event", "action", "irreversible"))
            assert all(word in impact.lower() for word in ("retain", "thread", "identity", "reserved"))
            assert "Private historical" not in impact
            await pool.execute("INSERT INTO chat_messages (session_id, message_id, role, content) "
                               "VALUES ($1, 'second-message', 'assistant', 'Another private body')", THREAD)
            changed = await domain.impact(server, operation, {"manager_id": TARGET}, api_v1.OPERATOR, {})
            assert "2 chat message" in changed and changed != impact
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 2
            assert await pool.fetchval("SELECT count(*) FROM manager_actions") == 1
    run_db(check, monkeypatch, timeout=30)


@pytest.mark.parametrize("manager_id", ["manager", "manager-missing"])
@pytest.mark.parametrize("action", ["preview", "purge"])
def test_default_and_missing_identity_refusals_preserve_neighboring_history(tmp_path, monkeypatch, manager_id, action):
    async def check(pool):
        await _seed(pool)
        async with _api(tmp_path, pool) as (call, _):
            status, answer = await call(action, {"manager_id": manager_id})
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
            assert await pool.fetchval("SELECT count(*) FROM llm_record_events") == 1
            assert await pool.fetchval("SELECT count(*) FROM manager_actions WHERE manager_id=$1", TARGET) == 1
    run_db(check, monkeypatch, timeout=30)


def test_live_manager_is_ineligible_and_purge_is_conflict_without_deletion(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        await pool.execute("UPDATE managers SET deleted_at=NULL, enabled=true WHERE id=$1", TARGET)
        async with _api(tmp_path, pool) as (call, _):
            status, answer = await call("preview")
            assert status == 200 and not answer["result"]["eligible"] and answer["result"]["deleted_at"] is None
            assert answer["result"]["counts"] == COUNTS and not answer["result"]["purged"]
            status, answer = await call("purge")
            assert status == 409 and answer["error"]["code"] == "conflict", answer
            assert await pool.fetchval("SELECT count(*) FROM chat_messages WHERE session_id=$1", THREAD) == 1
    run_db(check, monkeypatch, timeout=30)


def test_upper_retention_bound_is_accepted_using_database_age(tmp_path, monkeypatch):
    async def check(pool):
        await _seed(pool)
        await pool.execute("UPDATE managers SET deleted_at=transaction_timestamp()-interval '3651 days' WHERE id=$1", TARGET)
        async with _api(tmp_path, pool) as (call, _):
            for action in ("preview", "purge"):
                status, answer = await call(action, {"manager_id": TARGET, "retention_days": 3650})
                assert status == 200 and answer["result"]["retention_days"] == 3650, answer
                assert answer["result"]["eligible"] and answer["result"]["purged"] is (action == "purge")
    run_db(check, monkeypatch, timeout=30)


def _retired_fleet_with_bus(tmp_path):
    from looper.agents.bus import MessageBus
    from looper.agents.managers import ManagerFleet
    from looper.agents.tiers.manager import ManagerConfig

    bus = MessageBus(runtime_root=tmp_path)
    fleet = ManagerFleet(platform=SimpleNamespace(), supervisor=SimpleNamespace(_agents={}, bus=bus),
                         runtime_root=tmp_path, url="http://localhost:12345",
                         config=ManagerConfig(work_dir=tmp_path), broadcaster=None)
    return fleet, bus


@pytest.mark.parametrize("channel", ["web", "lark"])
def test_queued_outbound_reply_blocks_cleanup_until_settled(tmp_path, channel):
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address

    async def check():
        fleet, bus = _retired_fleet_with_bus(tmp_path)
        reply = MessageEnvelope.create(source=manager_address(TARGET), target=channel_address(channel),
                                       message_type=MessageType.AGENT_REPLY, payload={"text": "Public sample."})
        try:
            assert not fleet.cleanup_blockers(TARGET)
            await bus.publish(reply)
            assert bus.pending_count(TARGET) == 0
            assert bus.pending_count(reply.target.agent_id) == 1
            assert fleet.cleanup_blockers(TARGET)
            bus.settle(reply.message_id)
            assert not fleet.cleanup_blockers(TARGET)
        finally:
            await bus.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize("channel", ["web", "lark"])
def test_inflight_outbound_reply_blocks_cleanup_after_channel_dequeues_it(tmp_path, channel):
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address

    async def check():
        fleet, bus = _retired_fleet_with_bus(tmp_path)
        reply = MessageEnvelope.create(source=manager_address(TARGET), target=channel_address(channel),
                                       message_type=MessageType.AGENT_REPLY, payload={"text": "Public sample."})
        subscriber = bus.subscribe(reply.target.agent_id)
        try:
            await bus.publish(reply)
            assert await anext(subscriber) == reply
            assert bus.pending_count(reply.target.agent_id) == 0
            assert bus.pending_count(TARGET) == 0
            assert reply in bus.journal.undelivered()
            assert fleet.cleanup_blockers(TARGET)
            bus.settle(reply.message_id)
            assert not fleet.cleanup_blockers(TARGET)
        finally:
            await subscriber.aclose()
            await bus.shutdown()

    asyncio.run(check())


def test_neighbor_manager_outbound_reply_does_not_block_target_cleanup(tmp_path):
    from looper.agents.message import MessageEnvelope, MessageType, channel_address, manager_address

    async def check():
        fleet, bus = _retired_fleet_with_bus(tmp_path)
        neighbor = TARGET + "-neighbor"
        reply = MessageEnvelope.create(source=manager_address(neighbor), target=channel_address("web"),
                                       message_type=MessageType.AGENT_REPLY, payload={"text": "Public sample."})
        try:
            await bus.publish(reply)
            assert reply in bus.journal.undelivered()
            assert fleet.cleanup_blockers(neighbor)
            assert not fleet.cleanup_blockers(TARGET)
        finally:
            await bus.shutdown()

    asyncio.run(check())
